"""Tests for the M3 game driver (experiments/m3/driver.py).

Reference: docs/M3-DESIGN.md §4.3 (grammar-constrained legal moves) and §4.4
(curriculum vs. measuring stick). Engine-vs-engine play is fully deterministic
and needs no LLM, so the game loop / eval trace / terminal reasons are exercised
against `EngineAgent`. The `LLMAgent` path is exercised against a FAKE httpx
transport (a canned chat-completion JSON) — no real server is ever contacted.
"""

from __future__ import annotations

import json

import httpx
import pytest

from driver import (
    EngineAgent,
    GameRecord,
    LLMAgent,
    PlyEval,
    play_game,
)
from los_alamos import BLACK, WHITE, Board

_KNOWN_REASONS = {
    "checkmate",
    "stalemate",
    "insufficient-material",
    "fifty-move",
    "threefold",
    "max-plies",
}


# --- Engine vs engine: loop, trace shape, determinism -----------------------


def test_engine_selfplay_runs_and_trace_matches_moves() -> None:
    result = play_game(EngineAgent(2), EngineAgent(2), max_plies=80)
    assert isinstance(result, GameRecord)
    assert result.reason in _KNOWN_REASONS

    # Exactly one eval entry per move played.
    assert len(result.ply_evals) == len(result.moves)

    # The trace is a contiguous, correctly-sided, correctly-labelled record.
    for i, entry in enumerate(result.ply_evals):
        assert isinstance(entry, PlyEval)
        assert entry.ply_index == i
        assert entry.move_uci == result.moves[i]
        # White moves on even plies, Black on odd (White starts).
        assert entry.side == (WHITE if i % 2 == 0 else BLACK)
        assert isinstance(entry.eval_after, float)


def test_engine_selfplay_is_deterministic() -> None:
    a = play_game(EngineAgent(2), EngineAgent(2), max_plies=60)
    b = play_game(EngineAgent(2), EngineAgent(2), max_plies=60)
    assert a.moves == b.moves
    assert a.reason == b.reason
    assert a.winner == b.winner
    assert [e.eval_after for e in a.ply_evals] == [e.eval_after for e in b.ply_evals]


# --- Terminal reasons -------------------------------------------------------


def test_checkmate_reason_and_winner() -> None:
    # Black king a6 is mated: White queen a5 (defended by White king b4) gives
    # check along the a-file; a5/b5/b6 are all covered, so no escape and no
    # legal capture. Black to move => the game is already over.
    board = Board.from_piece_map({"b4": "K", "a5": "Q", "a6": "k"}, side=BLACK)
    assert board.is_checkmate()  # precondition sanity
    result = play_game(EngineAgent(2), EngineAgent(2), board=board)
    assert result.reason == "checkmate"
    assert result.winner == WHITE
    assert result.moves == []
    assert result.ply_evals == []


def test_stalemate_reason() -> None:
    # Black king a6 has no legal move and is NOT in check: White queen b4 covers
    # a5 (diagonal) and b5/b6 (file); the queen does not attack a6 itself.
    board = Board.from_piece_map({"e1": "K", "b4": "Q", "a6": "k"}, side=BLACK)
    assert board.is_stalemate()  # precondition sanity
    result = play_game(EngineAgent(1), EngineAgent(1), board=board)
    assert result.reason == "stalemate"
    assert result.winner is None


def test_insufficient_material_draw_reason() -> None:
    # Bare kings: a draw predicate (insufficient material) fires immediately.
    board = Board.from_piece_map({"a1": "K", "f6": "k"}, side=WHITE)
    assert board.is_insufficient_material()  # precondition sanity
    result = play_game(EngineAgent(1), EngineAgent(1), board=board)
    assert result.reason == "insufficient-material"
    assert result.winner is None


def test_max_plies_is_unfinished_not_a_draw() -> None:
    # A short cutoff from the opening: the game is not remotely over, so this must
    # be reported as an unfinished max-plies game, never as a silent draw.
    result = play_game(EngineAgent(1), EngineAgent(1), max_plies=4)
    assert result.reason == "max-plies"
    assert result.winner is None
    assert len(result.moves) == 4
    assert len(result.ply_evals) == 4


# --- LLMAgent against a fake transport --------------------------------------


def _fake_client(content: str, captured: dict[str, object] | None = None) -> httpx.Client:
    """An httpx.Client whose every POST returns a canned chat-completion whose
    message content is `content`. Optionally records the request payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_llm_agent_returns_legal_move() -> None:
    board = Board()  # initial position; "a2a3" is a legal opening push.
    captured: dict[str, object] = {}
    agent = LLMAgent(
        "http://test.invalid/v1/chat/completions",
        client=_fake_client("a2a3", captured),
    )
    move = agent.choose(board)
    assert move.uci() == "a2a3"
    # The request carried the per-position grammar (§4.3) that binds the output.
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert '"a2a3"' in payload["grammar"]


def test_llm_agent_strips_whitespace() -> None:
    board = Board()
    agent = LLMAgent("http://test.invalid", client=_fake_client("  a2a3\n"))
    assert agent.choose(board).uci() == "a2a3"


def test_llm_agent_raises_on_illegal_move() -> None:
    # Well-formed coordinates, but a2a5 is illegal (pawns push exactly one square).
    board = Board()
    agent = LLMAgent("http://test.invalid", client=_fake_client("a2a5"))
    with pytest.raises(ValueError):
        agent.choose(board)


def test_llm_agent_raises_on_garbage() -> None:
    board = Board()
    agent = LLMAgent("http://test.invalid", client=_fake_client("not-a-move"))
    with pytest.raises(ValueError):
        agent.choose(board)


def test_llm_agent_raises_on_empty_content() -> None:
    # Fail-Early: an empty completion (reasoning overran max_tokens) is not a move.
    board = Board()
    agent = LLMAgent("http://test.invalid", client=_fake_client(""))
    with pytest.raises(RuntimeError):
        agent.choose(board)


def test_llm_agent_refuses_terminal_position() -> None:
    board = Board.from_piece_map({"a1": "K", "f6": "k"}, side=WHITE)
    agent = LLMAgent("http://test.invalid", client=_fake_client("a1a2"))
    with pytest.raises(ValueError):
        agent.choose(board)
