"""Offline tests for the Tau-agent chess tools (experiments/m3/chess_agent_tools.py).

Drives each tool's async `execute` directly against a shared ChessGameState — no
LLM, no server. Verifies the JSON payloads, the play_move validate/terminate
contract, and the phase-gated toolset composition.
"""

from __future__ import annotations

import json

from chess_agent_tools import (
    AttackersDefendersTool,
    CheckStatusTool,
    ChessGameState,
    FenTool,
    ListLegalMovesTool,
    PieceInfoTool,
    PlayMoveTool,
    StaticEvalTool,
    move_phase_tools,
    review_phase_tools,
)
from los_alamos import WHITE, Board


def _payload(result: dict) -> object:
    assert result["is_error"] is False, result.get("error_message")
    return json.loads(result["content"][0]["text"])


async def test_list_legal_moves_tool() -> None:
    state = ChessGameState(board=Board())
    res = await ListLegalMovesTool(state).execute("tc1", {})
    assert len(_payload(res)) == 10


async def test_check_status_tool_names_checker() -> None:
    state = ChessGameState(board=Board.from_piece_map({"e1": "K", "e6": "r", "a6": "k"}, side=WHITE))
    payload = _payload(await CheckStatusTool(state).execute("tc1", {}))
    assert payload["in_check"] is True
    assert payload["checked_by"] == ["e6"]


async def test_piece_info_tool_reports_self_check_split() -> None:
    state = ChessGameState(
        board=Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    )
    payload = _payload(await PieceInfoTool(state).execute("tc1", {"square": "e2"}))
    assert set(payload["legal_targets"]) == {"e3", "e4", "e5", "e6"}
    assert set(payload["blocked_by_self_check"]) == {"a2", "b2", "c2", "d2", "f2"}


async def test_piece_info_tool_missing_arg_is_error() -> None:
    state = ChessGameState(board=Board())
    res = await PieceInfoTool(state).execute("tc1", {})
    assert res["is_error"] is True


async def test_piece_info_tool_empty_square_is_error() -> None:
    state = ChessGameState(board=Board())
    res = await PieceInfoTool(state).execute("tc1", {"square": "c3"})
    assert res["is_error"] is True and "no piece" in res["error_message"]


async def test_attackers_defenders_tool() -> None:
    state = ChessGameState(
        board=Board.from_piece_map({"e1": "K", "e2": "R", "e6": "r", "a6": "k"}, side=WHITE)
    )
    payload = _payload(await AttackersDefendersTool(state).execute("tc1", {"square": "e2"}))
    assert payload["white_attackers"] == ["e1"]
    assert payload["black_attackers"] == ["e6"]


async def test_fen_tool() -> None:
    state = ChessGameState(board=Board())
    assert _payload(await FenTool(state).execute("tc1", {})) == \
        "rnqknr/pppppp/6/6/PPPPPP/RNQKNR w - - 0 1"


async def test_static_eval_tool() -> None:
    state = ChessGameState(board=Board())
    payload = _payload(await StaticEvalTool(state).execute("tc1", {}))
    assert payload["material_margin_white"] == 0.0


async def test_play_move_legal_records_and_terminates() -> None:
    state = ChessGameState(board=Board())
    res = await PlayMoveTool(state).execute("tc1", {"move": "a2a3"})
    assert res["is_error"] is False
    assert res["terminate"] is True
    assert state.pending_move == "a2a3"


async def test_play_move_illegal_rejects_without_recording() -> None:
    state = ChessGameState(board=Board())
    res = await PlayMoveTool(state).execute("tc1", {"move": "a2a4"})  # 2-square push: illegal
    assert res["is_error"] is True
    assert state.pending_move is None  # nothing recorded — agent must retry


async def test_play_move_missing_arg_is_error() -> None:
    state = ChessGameState(board=Board())
    res = await PlayMoveTool(state).execute("tc1", {})
    assert res["is_error"] is True


def test_move_phase_toolset_composition() -> None:
    state = ChessGameState(board=Board())
    names = {t.name for t in move_phase_tools(state, with_eval=False)}
    assert "play_move" in names and "static_eval" not in names
    names_eval = {t.name for t in move_phase_tools(state, with_eval=True)}
    assert "static_eval" in names_eval


def test_review_phase_has_no_play_move() -> None:
    state = ChessGameState(board=Board())
    names = {t.name for t in review_phase_tools(state, with_eval=True)}
    assert "play_move" not in names
    assert "static_eval" in names
