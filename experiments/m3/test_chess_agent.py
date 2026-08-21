"""Offline dry-run of the agentic chess harness (experiments/m3/chess_agent.py).

Only the network boundary (`stream_simple`) is faked; the whole AgentSession loop,
the per-phase tool swap, play_move's validate+terminate, and the engine reply run
for real. The fake reads the LIVE board off the tools bound in context["tools"]
(they share the game's ChessGameState) and, in a move phase, submits the first
legal move via play_move; in an observe phase (no play_move tool) it just answers
with text. This proves the harness plays a legal, stateful, tool-using game with
no server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, ToolCall, Usage

from chess_agent import AgentGameConfig, play_agent_game
from los_alamos import BLACK, WHITE


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._i]
        self._i += 1
        return ev

    async def result(self) -> Any:
        for ev in self._events:
            if isinstance(ev, DoneEvent):
                return ev.final
        return None

    def abort(self) -> None:
        pass


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="local-llm",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[{"type": "text", "text": text}],
        api="openai-completions",
        provider="openai",
        model="local-llm",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


def _live_board(context: dict) -> Any:
    """Pull the shared ChessGameState.board off any state-bound tool in the wire."""
    for t in context.get("tools") or []:
        state = getattr(t, "state", None)
        if state is not None:
            return state.board
    raise AssertionError("no state-bound tool found in context['tools']")


async def test_agent_plays_a_full_legal_game_through_the_loop() -> None:
    calls = {"n": 0}

    async def fake(model, context, options=None):
        names = {getattr(t, "name", None) for t in (context.get("tools") or [])}
        if "play_move" in names:  # MOVE phase
            calls["n"] += 1
            board = _live_board(context)
            mv = board.legal_moves()[0].uci()
            final = _tool_call_assistant(f"c{calls['n']}", "play_move", {"move": mv})
            return _Stream([DoneEvent(final=final, usage=Usage())])
        # OBSERVE phase: no move, just text so the loop ends.
        final = _text_assistant("noted")
        return _Stream(
            [TextDeltaEvent(delta="noted", partial=final), DoneEvent(final=final, usage=Usage())]
        )

    config = AgentGameConfig(opponent_depth=1, max_plies=10, with_observe=True, max_turns=4)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        record = await play_agent_game(config)

    # The game reached a real terminal state within the ply budget.
    assert record.reason in {
        "checkmate",
        "stalemate",
        "insufficient-material",
        "fifty-move",
        "threefold",
        "max-plies",
    }
    assert record.winner in (WHITE, BLACK, None)
    # Plies alternate White/Black and every move is recorded with its eval.
    assert len(record.ply_evals) == len(record.moves)
    assert record.moves, "no moves were played"
    assert record.ply_evals[0].side == WHITE
    if len(record.ply_evals) > 1:
        assert record.ply_evals[1].side == BLACK
    # One agent move-phase per White ply; the fake submitted a legal move each time
    # (play_move validated it, so reaching here means no illegal move slipped through).
    white_plies = sum(1 for pe in record.ply_evals if pe.side == WHITE)
    assert len(record.move_tool_calls) == white_plies
    assert calls["n"] == white_plies


async def test_agent_move_only_mode_skips_observe() -> None:
    calls = {"n": 0}

    async def fake(model, context, options=None):
        names = {getattr(t, "name", None) for t in (context.get("tools") or [])}
        assert "play_move" in names, "move-only mode should never enter an observe phase"
        calls["n"] += 1
        board = _live_board(context)
        mv = board.legal_moves()[0].uci()
        final = _tool_call_assistant(f"c{calls['n']}", "play_move", {"move": mv})
        return _Stream([DoneEvent(final=final, usage=Usage())])

    config = AgentGameConfig(opponent_depth=1, max_plies=6, with_observe=False, max_turns=4)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        record = await play_agent_game(config)
    assert record.moves
