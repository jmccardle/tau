"""The M3 agentic chess harness (increment 2b): a Tau AGENT plays Los Alamos chess.

Reference: docs/M3-DESIGN.md §4.3/§4.4. This replaces the stateless single-shot
`driver.LLMAgent` with a persistent `tau_agent_core.AgentSession` that (a) reasons
with thinking ON, (b) queries board facts through tools instead of hallucinating
them, and (c) carries history across the game. One session per game; the toolset is
swapped per phase (move vs. review), which is also the seam for the static-eval
ablation.

Phases per full turn (the user's design):
  * MOVE    — the agent (White) analyses with fact tools and submits via play_move
              (which terminates the phase loop). Toolset = move_phase_tools.
  * (engine — Black replies with the deterministic opponent; no LLM.)
  * OBSERVE — the agent assesses the resulting position with fact tools (no move).
              Toolset = review_phase_tools. Optional (config `with_observe`).

Memory injection + the strategize/write phase are a later increment; this one
proves the agent plays a legal, tool-using, stateful game. Fail-Early: if the
agent never submits a legal move within the turn budget, we RAISE — we do not pick
a move for it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_ai.types import Model

import chess_agent_tools as cat
from driver import PlyEval, _render_board
from los_alamos import (
    BLACK,
    WHITE,
    Board,
    parse_move,
)

_RULES = (
    "You are playing Los Alamos chess: a 6x6 variant on files a-f, ranks 1-6. "
    "There are NO bishops. The queen keeps full orthogonal+diagonal movement; rooks "
    "move orthogonally, knights the L, kings one square. Pawns move forward exactly "
    "ONE square (no two-square first move), capture diagonally, no en passant, and "
    "promote on the last rank to Q/R/N. There is no castling."
)

_SYSTEM_PROMPT = (
    _RULES + "\n\n"
    "You are White. You have TOOLS that compute the board state exactly — use them; "
    "do not guess. Before you move: call check_status to see if you are in check, "
    "list_legal_moves to see your options, and piece_info / attackers_defenders to "
    "check whether a piece is hanging or a destination square is defended. Think, "
    "then submit exactly one legal move with play_move. Play to win material and "
    "protect your king."
)


@dataclass
class AgentGameRecord:
    """A played agent game: mirrors driver.GameRecord + per-move tool-call counts."""

    winner: str | None
    reason: str
    moves: list[str] = field(default_factory=list)
    ply_evals: list[PlyEval] = field(default_factory=list)
    move_tool_calls: list[int] = field(default_factory=list)  # tool calls per agent move

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentGameConfig:
    opponent_depth: int = 1
    max_plies: int = 50
    with_observe: bool = True
    with_eval_move: bool = False  # static_eval tool available in the move phase?
    with_eval_review: bool = False  # ... in the review phase?
    max_turns: int = 20  # per-phase agent-loop turn cap (the model explores facts thoroughly)
    base_url: str = "http://127.0.0.1:8080/v1"
    model_id: str = "local-llm"
    api_key: str = "local"
    reasoning: str | None = "medium"  # request thinking ON


def _model(config: AgentGameConfig) -> Model:
    return Model(
        id=config.model_id,
        name=config.model_id,
        api="openai-completions",
        provider="openai",
        base_url=config.base_url,
        context_window=32000,
        max_tokens=8000,
    )


def _terminal(board: Board, plies: int, max_plies: int) -> tuple[bool, str | None, str]:
    """(is_over, winner, reason). Winner is the side NOT to move on checkmate."""
    if board.is_checkmate():
        winner = BLACK if board.side == WHITE else WHITE
        return True, winner, "checkmate"
    if board.is_stalemate():
        return True, None, "stalemate"
    if board.is_insufficient_material():
        return True, None, "insufficient-material"
    if board.is_fifty_move_draw():
        return True, None, "fifty-move"
    if board.is_threefold_repetition():
        return True, None, "threefold"
    if plies >= max_plies:
        return True, None, "max-plies"
    return False, None, "in-progress"


def _move_prompt(board: Board, last_black: str | None) -> str:
    parts = []
    if last_black is not None:
        parts.append(f"Black just played {last_black}.")
    parts.append("Current position (uppercase = White = you, lowercase = Black):")
    parts.append(_render_board(board))
    parts.append(
        "It is your move. Analyse with your tools, then submit exactly one legal move "
        "with play_move."
    )
    return "\n".join(parts)


def _observe_prompt(board: Board, black_move: str) -> str:
    return "\n".join([
        f"Black replied {black_move}. New position:",
        _render_board(board),
        "Briefly assess the position using your tools: are any of your pieces now "
        "attacked or hanging? Do not move; just report what changed.",
    ])


async def play_agent_game(
    config: AgentGameConfig,
    *,
    board: Board | None = None,
) -> AgentGameRecord:
    """Play one game: the Tau agent (White) vs the deterministic engine (Black)."""
    board = board if board is not None else Board()
    state = cat.ChessGameState(board=board)
    session = AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(config),
        system_prompt=_SYSTEM_PROMPT,
        tools=[],
        api_key=config.api_key,
        reasoning=config.reasoning,
        max_turns=config.max_turns,
    )

    moves: list[str] = []
    ply_evals: list[PlyEval] = []
    move_tool_calls: list[int] = []
    last_black: str | None = None
    plies = 0

    def _record(side: str, uci: str) -> None:
        ply_evals.append(
            PlyEval(ply_index=len(moves) - 1, side=side, move_uci=uci, eval_after=board.evaluate())
        )

    while True:
        over, winner, reason = _terminal(board, plies, config.max_plies)
        if over:
            return AgentGameRecord(
                winner=winner, reason=reason, moves=moves,
                ply_evals=ply_evals, move_tool_calls=move_tool_calls,
            )

        # --- MOVE phase (agent, White) ---
        state.pending_move = None
        session._tools = cat.move_phase_tools(state, with_eval=config.with_eval_move)
        turn_msgs = await session.prompt(_move_prompt(board, last_black))
        move_tool_calls.append(_count_tool_calls(turn_msgs))
        if state.pending_move is None:
            raise RuntimeError(
                f"agent submitted no legal move within {config.max_turns} turns "
                f"(ply {plies}); Fail-Early rather than choosing for it"
            )
        mv = parse_move(board, state.pending_move)
        board.push(mv)
        moves.append(state.pending_move)
        _record(WHITE, state.pending_move)
        plies += 1

        over, winner, reason = _terminal(board, plies, config.max_plies)
        if over:
            return AgentGameRecord(
                winner=winner, reason=reason, moves=moves,
                ply_evals=ply_evals, move_tool_calls=move_tool_calls,
            )

        # --- engine reply (Black) ---
        black_mv = board.best_move(config.opponent_depth)
        board.push(black_mv)
        moves.append(black_mv.uci())
        _record(BLACK, black_mv.uci())
        last_black = black_mv.uci()
        plies += 1

        # --- OBSERVE phase (agent reflects; no move) ---
        if config.with_observe and not _terminal(board, plies, config.max_plies)[0]:
            session._tools = cat.review_phase_tools(state, with_eval=config.with_eval_review)
            await session.prompt(_observe_prompt(board, last_black))


def _count_tool_calls(messages: list) -> int:
    """Count toolResult messages in a prompt()'s returned messages (== tools run)."""
    n = 0
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "toolResult":
            n += 1
    return n
