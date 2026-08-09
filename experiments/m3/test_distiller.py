"""Tests for the per-game strategy distiller (experiments/m3/distiller.py).

Reference: docs/M3-DESIGN.md §4.2 (per-game distillation of swung moves into reusable
lessons) and §6 (C1 self-induction). The LLM is a FAKE httpx transport (mirroring
test_driver.py's ``_fake_client``); no GPU, no server. The eval-swing plumbing is
hand-synthesised, and the game record is a handful of real legal moves so the distiller's
board replay is exercised. Sibling imports mirror the other experiments/m3 tests (pytest
puts the directory on sys.path).
"""

from __future__ import annotations

import json

import httpx
import pytest

from credit import Swing
from distiller import DISTILL_PROMPT, Distiller, _parse_lessons
from driver import GameRecord, PlyEval
from los_alamos import BLACK, WHITE, Board


def _fake_client(content: str, captured: dict[str, object] | None = None) -> httpx.Client:
    """An httpx.Client whose every POST returns a canned completion of ``content``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _game() -> tuple[GameRecord, list[Swing], Board]:
    """A short, real 4-ply game record + one White swung move at ply 0."""
    board = Board()
    moves: list[str] = []
    ply_evals: list[PlyEval] = []
    for i in range(4):
        mv = board.legal_moves()[0]
        uci = mv.uci()
        board.push(mv)
        moves.append(uci)
        ply_evals.append(
            PlyEval(
                ply_index=i,
                side=WHITE if i % 2 == 0 else BLACK,
                move_uci=uci,
                eval_after=board.evaluate(),
            )
        )
    record = GameRecord(winner=WHITE, reason="checkmate", moves=moves, ply_evals=ply_evals)
    swung = [Swing(ply_index=0, side=WHITE, move_uci=moves[0], swing=-2.0)]
    return record, swung, Board()


# --- parsing ----------------------------------------------------------------------


def test_distill_parses_numbered_list() -> None:
    record, swung, start = _game()
    distiller = Distiller("http://test.invalid", client=_fake_client("1. lesson A\n2. lesson B"))
    assert distiller.distill(record, swung, start) == ["lesson A", "lesson B"]


def test_parse_lessons_handles_bullets_and_blank_lines() -> None:
    parsed = _parse_lessons("- keep the queen safe\n\n* contest the center\n1) trade when ahead")
    assert parsed == ["keep the queen safe", "contest the center", "trade when ahead"]


# --- Fail-Early ------------------------------------------------------------------


def test_empty_completion_raises() -> None:
    record, swung, start = _game()
    distiller = Distiller("http://test.invalid", client=_fake_client(""))
    with pytest.raises(RuntimeError, match="empty content"):
        distiller.distill(record, swung, start)


def test_unparseable_completion_raises() -> None:
    record, swung, start = _game()
    # Non-empty, but every line is a bare list marker — no lesson survives parsing.
    distiller = Distiller("http://test.invalid", client=_fake_client("-\n*"))
    with pytest.raises(RuntimeError, match="no parseable lessons"):
        distiller.distill(record, swung, start)


def test_empty_swung_raises_without_calling_the_model() -> None:
    record, _swung, start = _game()
    distiller = Distiller("http://test.invalid", client=_fake_client("1. never reached"))
    with pytest.raises(ValueError, match="no swung moves"):
        distiller.distill(record, [], start)


def test_swung_spanning_sides_raises() -> None:
    record, swung, start = _game()
    mixed = swung + [Swing(ply_index=1, side=BLACK, move_uci=record.moves[1], swing=-1.0)]
    distiller = Distiller("http://test.invalid", client=_fake_client("1. never reached"))
    with pytest.raises(ValueError, match="multiple sides"):
        distiller.distill(record, mixed, start)


# --- prompt construction ---------------------------------------------------------


def test_prompt_carries_rules_outcome_swing_and_board() -> None:
    record, swung, start = _game()
    captured: dict[str, object] = {}
    distiller = Distiller("http://test.invalid", client=_fake_client("1. x", captured))
    distiller.distill(record, swung, start)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    content = payload["messages"][0]["content"]
    # Variant rules (so the model does not distil standard-chess priors), the outcome, the
    # swung move + its MISTAKE tag (negative swing), and a rendered board are all present.
    assert "Los Alamos" in content
    assert "White WON by checkmate" in content
    assert swung[0].move_uci in content
    assert "MISTAKE" in content
    assert "a b c d e f" in content  # the board diagram footer
    # No grammar is sent — the lessons are free-form (§4.3 grammar is for MOVES, not this).
    assert "grammar" not in payload


def test_distill_prompt_is_a_reviewable_constant() -> None:
    # The owner reviews DISTILL_PROMPT before a GPU run; guard its load-bearing pieces.
    for token in ("{side}", "{outcome}", "{swings}", "GENERALIZABLE", "numbered list"):
        assert token in DISTILL_PROMPT
