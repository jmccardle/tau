"""M3 game driver: the trajectory generator for the Los Alamos chess experiment.

Reference: docs/M3-DESIGN.md §4.3 (grammar-constrained legal moves — every LLM
turn is bound to a per-position GBNF `root ::= "<move>" | ...`, so an induced
rule that *would* emit an illegal move is simply never emittable) and §4.4
(curriculum vs. measuring stick — self-play / play-vs-greedy *generate*
trajectories, a fixed deterministic opponent ladder *measures*).

This module plays games between two `Agent`s (LLM vs engine, LLM vs LLM, engine
vs engine) and records the per-ply eval trace that the downstream
credit-assignment / induction steps consume: the eval-swing signal of §4.2/§7 is
read straight off `GameRecord.ply_evals`.

Fail-Early (repo rule): there is NO illegal-move fallback. An `LLMAgent` that is
handed a string which is not a legal move lets `parse_move` raise; a max-plies
cutoff is recorded as an unfinished game, never scored as a silent draw.

Depends only on the stdlib + httpx (the LLM-call pattern mirrors
experiments/m2/alpha_selector.py). The driver does NOT manage the inference
server; it assumes a running OpenAI-compatible endpoint launched server-side with
`--reasoning-budget N` (so a grammar-bound token is actually emitted after the
end-of-think tag) and just sends grammar + a high max_tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from los_alamos import (
    BLACK,
    BOARD_SIZE,
    WHITE,
    Board,
    Move,
    format_move,
    grammar_for_position,
    legal_move_strings,
    parse_move,
    square,
)

# --- Records ----------------------------------------------------------------


@dataclass(frozen=True)
class PlyEval:
    """One half-move's contribution to the dense credit-assignment signal (§4.2).

    `eval_after` is `board.evaluate()` in the White-positive convention, measured
    AFTER the move was pushed. The eval-swing for a ply is the difference between
    consecutive `eval_after` values (with the first swing measured against the
    starting position's eval); §7's conditional gate consumes that swing crossed
    with which strategy was used.
    """

    ply_index: int  # 0-based half-move number.
    side: str  # WHITE or BLACK — the side that made this move.
    move_uci: str  # coordinate notation, e.g. "a2a3" / "a5a6q".
    eval_after: float  # White-positive static eval AFTER the push.


@dataclass
class GameRecord:
    """Everything a played game produces for measurement + distillation.

    A richer record than `los_alamos.GameResult`: it keeps that type's
    `winner`/`reason`/`moves` and adds the per-ply eval trace. `winner` is
    WHITE / BLACK / None (None = draw or unfinished); `reason` is one of
    ``checkmate``, ``stalemate``, ``insufficient-material``, ``fifty-move``,
    ``threefold``, or ``max-plies``. ``max-plies`` marks an UNFINISHED game — it
    is not a draw and must not be scored as one.
    """

    winner: str | None
    reason: str
    moves: list[str] = field(default_factory=list)
    ply_evals: list[PlyEval] = field(default_factory=list)


# --- Agents -----------------------------------------------------------------


class Agent(Protocol):
    """A player: given the side-to-move's `board`, return a legal `Move`.

    `play_game` is agent-agnostic; it only calls `choose`. Implementations must
    return a move that is legal in the given position (the engine's own
    `best_move` / `parse_move` guarantee this).
    """

    def choose(self, board: Board) -> Move: ...


class EngineAgent:
    """The deterministic opponent ladder (§4.4).

    depth 1 is the greedy (one-ply material+eval) floor; depths 2-4 are
    alpha-beta minimax on the same static eval. `best_move` is deterministic
    (fixed generation order, strict first-best-wins tie-break), so this agent is
    the reproducible measuring stick.
    """

    def __init__(self, depth: int) -> None:
        if depth < 1:
            raise ValueError("EngineAgent depth must be >= 1")
        self.depth = depth

    def choose(self, board: Board) -> Move:
        return board.best_move(self.depth)


# The prompt the LLM sees. The grammar — not the prose — is what makes the output
# a legal move; the prose gives the model the position and the reasoning target.
_SYSTEM_PREFACE = (
    "You are playing Los Alamos chess: a 6x6 variant on files a-f, ranks 1-6. "
    "There are NO bishops. The queen keeps full orthogonal+diagonal movement; "
    "rooks move orthogonally, knights the L, kings one square. Pawns move forward "
    "exactly ONE square (no two-square first move), capture diagonally, have no "
    "en passant, and promote on the last rank to Q/R/N (no bishop). There is no "
    "castling. Reply with exactly one move in coordinate notation, e.g. e2e3 or "
    "a5a6q for a promotion."
)


class LLMAgent:
    """A grammar-constrained LLM player (§4.3).

    Each turn it builds `grammar_for_position(board)` (the per-position GBNF that
    binds the completion to exactly the legal moves), POSTs a chat/completions
    request with that grammar and thinking left ON (mirroring
    experiments/m2/alpha_selector.py — the server must be launched with
    `--reasoning-budget N` for the end-of-think tag to fire), extracts the
    completion text, and `parse_move`s it into a legal `Move`.

    Fail-Early: if the completion is empty (reasoning ran past `max_tokens`) or is
    not a legal move, this RAISES rather than substituting a default. The
    optional `strategy_context` is the retrieval read-back injected into the
    prompt as a plain string; the store/motif wiring is a later step, not this
    one. An optional `client` may be injected for testing (a real one is created
    per call otherwise).
    """

    def __init__(
        self,
        endpoint: str,
        *,
        strategy_context: str | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.0,
        timeout: float = 600.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.strategy_context = strategy_context
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._client = client

    def _build_prompt(self, board: Board) -> str:
        mover = "White" if board.side == WHITE else "Black"
        legal = ", ".join(legal_move_strings(board))
        parts = [
            _SYSTEM_PREFACE,
            f"\nIt is {mover} to move.",
            "Board (uppercase = White, lowercase = Black, '.' = empty):",
            _render_board(board),
        ]
        if self.strategy_context is not None:
            parts.append(f"\nStrategy notes:\n{self.strategy_context}")
        parts.append(f"\nLegal moves: {legal}")
        parts.append("Choose one legal move and reply with it in coordinate notation.")
        return "\n".join(parts)

    def choose(self, board: Board) -> Move:
        # A terminal position has no move to choose; do not call the model.
        # (grammar_for_position also raises on no legal moves, but a draw-terminal
        # position such as insufficient material still HAS legal moves, so guard
        # explicitly on the game-over predicate.)
        if board.is_game_over():
            raise ValueError("cannot choose a move: position is terminal")
        grammar = grammar_for_position(board)
        payload = {
            "messages": [{"role": "user", "content": self._build_prompt(board)}],
            "grammar": grammar,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        client = self._client or httpx.Client()
        try:
            response = client.post(self.endpoint, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        finally:
            if self._client is None:
                client.close()
        content = (data["choices"][0]["message"]["content"] or "").strip()
        if not content:
            # Fail-Early: a truncated-before-answer completion is not a move. Do
            # NOT fabricate one — that would poison the very trajectory we record.
            raise RuntimeError(
                "LLM returned empty content — reasoning likely ran past max_tokens "
                "before emitting a grammar-bound move"
            )
        # parse_move RAISES on a malformed or illegal string; let it propagate.
        return parse_move(board, content)


def _render_board(board: Board) -> str:
    """A plain ASCII rendering, rank 6 at the top down to rank 1."""
    lines = []
    for rank in range(BOARD_SIZE - 1, -1, -1):
        row = " ".join(board.squares[square(f, rank)] for f in range(BOARD_SIZE))
        lines.append(f"{rank + 1}  {row}")
    lines.append("   " + " ".join("abcdef"[:BOARD_SIZE]))
    return "\n".join(lines)


# --- Game loop --------------------------------------------------------------


def play_game(
    white: Agent,
    black: Agent,
    *,
    board: Board | None = None,
    max_plies: int = 200,
) -> GameRecord:
    """Play `white` vs `black` and record the per-ply eval trace.

    Loop: while the position is not terminal and fewer than `max_plies`
    half-moves have been played, ask the side-to-move's agent for a move, push
    it, and record a `PlyEval(ply_index, side, move_uci, eval_after)` where
    `eval_after = board.evaluate()` (White-positive) AFTER the push. The trace is
    the eval-swing signal of §4.2/§7.

    Terminal `winner`/`reason` come straight from the engine's predicates. A
    `max_plies` cutoff is recorded as `reason="max-plies"` with `winner=None` — an
    UNFINISHED game, never a silent draw.
    """
    if board is None:
        board = Board()
    agents: dict[str, Agent] = {WHITE: white, BLACK: black}

    moves: list[str] = []
    ply_evals: list[PlyEval] = []
    ply = 0
    while not board.is_game_over() and ply < max_plies:
        side = board.side
        move = agents[side].choose(board)
        board.push(move)
        uci = format_move(move)
        moves.append(uci)
        ply_evals.append(
            PlyEval(
                ply_index=ply,
                side=side,
                move_uci=uci,
                eval_after=board.evaluate(),
            )
        )
        ply += 1

    winner, reason = _terminal_status(board, ply, max_plies)
    return GameRecord(winner=winner, reason=reason, moves=moves, ply_evals=ply_evals)


def _terminal_status(board: Board, plies: int, max_plies: int) -> tuple[str | None, str]:
    """Winner + reason from the engine's terminal predicates.

    Checkmate is tested before the draw predicates (both hinge on "no legal
    moves"); a real terminal always resolves here, so `max-plies` is reported only
    for a genuinely unfinished game. The final raise is a Fail-Early guard that
    should be unreachable.
    """
    if board.is_checkmate():
        # The side to move is mated; the other side won.
        loser = board.side
        winner = BLACK if loser == WHITE else WHITE
        return winner, "checkmate"
    if board.is_stalemate():
        return None, "stalemate"
    if board.is_insufficient_material():
        return None, "insufficient-material"
    if board.is_fifty_move_draw():
        return None, "fifty-move"
    if board.is_threefold_repetition():
        return None, "threefold"
    if plies >= max_plies:
        return None, "max-plies"
    raise RuntimeError(
        "play_game loop exited with a non-terminal position below max_plies "
        "(internal invariant violated)"
    )
