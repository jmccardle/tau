"""Tau-agent tool wrappers for the M3 agentic chess experiment (increment 2a).

Reference: docs/M3-DESIGN.md §4.3. These wrap the deterministic primitives in
`chess_tools.py` as `tau_agent_core` tools (duck-typed: name/label/description/
parameters/execution_mode + async `execute`), so a ChessAgent queries board facts
instead of hallucinating them (the smoke probe showed the raw model is
threat-blind). All tools read/write ONE shared `ChessGameState.board`, so the same
tool instances stay correct as the live game advances.

Tool taxonomy (attribution-preserving, per the design):
  * FACT tools (always available): legal moves, piece_info, attackers/defenders,
    check status, fen — they return ground-truth facts, never decisions.
  * ACTION tool: `play_move` — the agent's move-submission channel. It validates
    the move (Fail-Early: an illegal move is an error the agent must correct, not a
    silent pass) and TERMINATES the phase loop once a legal move is submitted.
  * `static_eval` — the phase-gated ABLATION tool; the harness includes it in the
    move-phase and/or review-phase toolset, or not at all. No best-move oracle
    exists anywhere, so any improvement stays attributable to reasoning + memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from tau_agent_core.tools.base import AgentToolResult

import chess_tools
from los_alamos import Board, parse_move


@dataclass
class ChessGameState:
    """The live position the tools operate on, shared by every tool in a game.

    The harness advances `board` (agent move, then engine reply); tools read it at
    call time. `pending_move` is where `play_move` records the agent's validated
    choice for the harness to apply after the move-phase loop terminates.
    """

    board: Board
    pending_move: str | None = None
    move_history: list[str] = field(default_factory=list)


def _ok(
    tool_name: str, tool_call_id: str, payload: Any, *, terminate: bool = False
) -> dict[str, Any]:
    """A success result whose single text block is the JSON-encoded payload."""
    result: dict[str, Any] = AgentToolResult(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        content=[{"type": "text", "text": json.dumps(payload)}],
        terminate=terminate,
    ).model_dump()
    return result


def _err(tool_name: str, tool_call_id: str, message: str) -> dict[str, Any]:
    result: dict[str, Any] = AgentToolResult.from_error(
        tool_name=tool_name, error_message=message, tool_call_id=tool_call_id
    ).model_dump()
    return result


class _StateTool:
    """Base for tools bound to a shared ChessGameState."""

    name: str = ""
    label: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    execution_mode: str = "parallel"

    def __init__(self, state: ChessGameState) -> None:
        self.state = state


class ListLegalMovesTool(_StateTool):
    name = "list_legal_moves"
    label = "List Legal Moves"
    description = (
        "Return every legal move for the side to move, in coordinate notation "
        "(e.g. e2e3, a5a6q). Use this to see your full set of options."
    )

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        return _ok(self.name, tool_call_id, chess_tools.list_legal_moves(self.state.board))


class CheckStatusTool(_StateTool):
    name = "check_status"
    label = "Check Status"
    description = (
        "Report whether the side to move is in check, the king's square, and which "
        "enemy pieces are giving check. Consult this before every move."
    )

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        return _ok(self.name, tool_call_id, chess_tools.check_status(self.state.board))


class PieceInfoTool(_StateTool):
    name = "piece_info"
    label = "Piece Info"
    description = (
        "Given a square (e.g. 'd1'), report the piece there, the squares it attacks, "
        "its legal target squares, the squares it could reach but for leaving its own "
        "king in check (blocked_by_self_check), and which enemy/friendly pieces attack "
        "and defend it. Use this to check if a piece is hanging or pinned."
    )
    parameters = {
        "type": "object",
        "properties": {"square": {"type": "string", "description": "Square, e.g. 'd1'"}},
        "required": ["square"],
    }

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        square = args.get("square")
        if not square:
            return _err(self.name, tool_call_id, "Missing required argument: 'square'")
        try:
            return _ok(self.name, tool_call_id, chess_tools.piece_info(self.state.board, square))
        except ValueError as e:
            return _err(self.name, tool_call_id, str(e))


class AttackersDefendersTool(_StateTool):
    name = "attackers_defenders"
    label = "Attackers & Defenders"
    description = (
        "Given a square, list the white and black pieces that attack (or defend) it. "
        "Use this to evaluate whether a capture or a move into a square is safe."
    )
    parameters = {
        "type": "object",
        "properties": {"square": {"type": "string", "description": "Square, e.g. 'd4'"}},
        "required": ["square"],
    }

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        square = args.get("square")
        if not square:
            return _err(self.name, tool_call_id, "Missing required argument: 'square'")
        try:
            return _ok(
                self.name,
                tool_call_id,
                chess_tools.attackers_defenders(self.state.board, square),
            )
        except ValueError as e:
            return _err(self.name, tool_call_id, str(e))


class FenTool(_StateTool):
    name = "fen"
    label = "Board FEN"
    description = "Return the current position as a FEN string (6x6 Los Alamos variant)."

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        return _ok(self.name, tool_call_id, chess_tools.board_to_fen(self.state.board))


class StaticEvalTool(_StateTool):
    """ABLATION tool — included per-phase by the harness, not always on."""

    name = "static_eval"
    label = "Static Evaluation"
    description = (
        "Return the engine's static evaluation of the current position (White-positive) "
        "and the raw material tally for each side. A positive value favours White."
    )

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        return _ok(self.name, tool_call_id, chess_tools.static_eval(self.state.board))


class PlayMoveTool(_StateTool):
    """The agent's move-submission channel. Validates and terminates the phase."""

    name = "play_move"
    label = "Play Move"
    description = (
        "Submit your chosen move in coordinate notation (e.g. e2e3, or a5a6q to promote). "
        "The move must be legal in the current position; an illegal move is rejected and "
        "you must choose again. Submitting a legal move ends your turn."
    )
    parameters = {
        "type": "object",
        "properties": {
            "move": {"type": "string", "description": "The move in coordinate notation, e.g. e2e3"}
        },
        "required": ["move"],
    }

    async def execute(
        self,
        tool_call_id: str,
        args: dict,
        signal: Any = None,
        on_update: Callable | None = None,
    ) -> dict:
        move = args.get("move")
        if not move:
            return _err(self.name, tool_call_id, "Missing required argument: 'move'")
        try:
            parse_move(self.state.board, move)  # raises on illegal/malformed
        except ValueError as e:
            return _err(
                self.name,
                tool_call_id,
                f"Illegal move {move!r}: {e}. Call list_legal_moves and choose a legal one.",
            )
        self.state.pending_move = move
        return _ok(
            self.name,
            tool_call_id,
            {"accepted": move, "message": f"Move {move} recorded."},
            terminate=True,
        )


# Fact tools always available; play_move is the action; static_eval is gated.
FACT_TOOL_CLASSES: tuple[type[_StateTool], ...] = (
    ListLegalMovesTool,
    CheckStatusTool,
    PieceInfoTool,
    AttackersDefendersTool,
    FenTool,
)


def move_phase_tools(state: ChessGameState, *, with_eval: bool = False) -> list[_StateTool]:
    """Toolset for the move phase: facts + play_move (+ static_eval if ablation on)."""
    tools: list[_StateTool] = [cls(state) for cls in FACT_TOOL_CLASSES]
    if with_eval:
        tools.append(StaticEvalTool(state))
    tools.append(PlayMoveTool(state))
    return tools


def review_phase_tools(state: ChessGameState, *, with_eval: bool = False) -> list[_StateTool]:
    """Toolset for the review phase: facts only (+ static_eval if gated to review).

    No play_move — the review phase reflects, it does not move.
    """
    tools: list[_StateTool] = [cls(state) for cls in FACT_TOOL_CLASSES]
    if with_eval:
        tools.append(StaticEvalTool(state))
    return tools
