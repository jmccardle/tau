"""The per-game strategy distiller for the M3 Los Alamos chess experiment.

Reference: docs/M3-DESIGN.md §4.2 (attribute a lesson to the moves that SWUNG the eval,
learning from swung-toward successes and swung-away failures alike — the ReasoningBank
success-and-failure discipline) and §6 (C1 = self-induced: distill after each task,
retrieve before). This module is the "distill after" half: ONE LLM call per finished game
that turns the game's outcome plus its swung moves into 1-3 GENERALIZABLE lessons.

It is per-GAME distillation, not per-move: the whole game (outcome + the handful of moves
that moved the eval past the threshold) goes to the model in a single request, and the
model is asked for reusable heuristics for positions LIKE THESE — not a running commentary
on individual moves. The swung moves are computed upstream by ``credit.swung_moves`` and
filtered to the learner's own side before being handed here.

Fail-Early (repo rule): if the model returns empty/unparseable content, or if it is handed
no swung moves to reason about, this RAISES rather than fabricating a lesson — a fabricated
lesson would poison the very store the experiment measures. Thinking is left ON (the server
must be launched with ``--reasoning-budget N``; the α-selector method), and NO grammar is
used: the lessons are free-form prose parsed into a list client-side.

Stdlib + httpx only. Sibling imports mirror the other experiments/m3 modules (pytest puts
the directory on sys.path).
"""

from __future__ import annotations

import re

import httpx

from credit import Swing
from driver import GameRecord
from los_alamos import (
    BLACK,
    BOARD_SIZE,
    WHITE,
    Board,
    parse_move,
    square,
)

# Terminal reasons that denote a genuine finished DRAW. `max-plies` is deliberately absent
# (it marks an UNFINISHED game, driver.py / §4.4) and is reported as such to the model.
_DRAW_REASONS = frozenset({"stalemate", "insufficient-material", "fifty-move", "threefold"})


# =====================================================================================
# THE PROMPT. The repo owner reviews this before it runs on GPU. `{side}`, `{outcome}`,
# and `{swings}` are filled per game; everything else is fixed. The variant rules are
# stated up front so the model distils lessons about THIS game, not standard-chess priors
# (those priors are the built-in poison of §4.5, not something the distiller should invent).
# =====================================================================================
DISTILL_PROMPT = (
    "You are analysing one finished game of Los Alamos chess to distil reusable "
    "strategic lessons.\n\n"
    "Los Alamos chess is a 6x6 variant on files a-f, ranks 1-6, with NO bishops. The "
    "queen keeps full orthogonal+diagonal movement; rooks move orthogonally, knights the "
    "L, kings one square. Pawns move forward exactly ONE square (no two-square first "
    "move), capture diagonally, have no en passant, and promote on the last rank to "
    "Q/R/N. There is no castling. Standard-chess opening theory does NOT transfer.\n\n"
    "The game below is shown from {side}'s point of view. After the result you are given "
    "the moves that most changed the balance — the 'swing' in evaluation points, signed "
    "from {side}'s perspective: a NEGATIVE swing is a mistake that lost ground, a POSITIVE "
    "swing is a strong move that gained ground. Learn from BOTH the mistakes and the "
    "strong moves.\n\n"
    "{outcome}\n\n"
    "Swing moves (the board shown is the position BEFORE the move was played):\n"
    "{swings}\n\n"
    "Write 1 to 3 GENERALIZABLE lessons: short imperative heuristics that would help "
    "{side} play positions LIKE THESE better next time. Each lesson must be a general "
    "principle — a rule of thumb about piece activity, king safety, material, tempo, pawn "
    "structure, or tactics in THIS variant — NOT a remark about one specific move or "
    "square, and not a restatement of the moves. Reply with a numbered list, one lesson "
    "per line, and nothing else."
)


class Distiller:
    """Turns a finished ``GameRecord`` + its swung moves into a list of lesson strings.

    One LLM call per game (§4.2/§6). ``distill`` builds :data:`DISTILL_PROMPT` from the
    game's outcome and swung moves, POSTs it (thinking ON, no grammar — the α-selector
    method), and parses the free-form numbered list of lessons out of the completion.

    An optional ``client`` may be injected for testing (a real one is created per call
    otherwise), mirroring ``driver.LLMAgent``.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        max_tokens: int = 8000,
        temperature: float = 0.0,
        timeout: float = 600.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._client = client

    def distill(
        self,
        game_record: GameRecord,
        swung: list[Swing],
        board_start: Board,
    ) -> list[str]:
        """Distil 1-3 generalizable lessons from one game's swung moves (§4.2).

        ``swung`` are the distillation targets (from ``credit.swung_moves``), already
        filtered to the LEARNER's own side — a lesson is only attributable to a move the
        learner made. ``board_start`` is the position the game began from; the record's
        moves are replayed over a copy of it to render each swung move's pre-move board.

        Fail-Early:
          * an empty ``swung`` (no attributable moves) has nothing to distil and RAISES —
            the caller must skip the LLM call, not send it an empty target;
          * swung moves from more than one side are a caller bug (the learner has one
            side) and RAISE;
          * empty or unparseable model output RAISES rather than yielding a fabricated
            lesson.
        """
        if not swung:
            raise ValueError(
                "distill: no swung moves to distil from — the caller must not invoke the "
                "distiller for a game with no attributable moves"
            )
        sides = {s.side for s in swung}
        if len(sides) != 1:
            raise ValueError(
                f"distill: swung moves span multiple sides {sorted(sides)!r}; the learner "
                "has exactly one side — filter to it before distilling"
            )
        side = swung[0].side
        prompt = DISTILL_PROMPT.format(
            side=_side_name(side),
            outcome=_outcome_line(game_record, side),
            swings=_render_swings(game_record, swung, board_start),
        )
        content = self._complete(prompt)
        lessons = _parse_lessons(content)
        if not lessons:
            # Fail-Early: a non-empty completion that carried no parseable lesson is not a
            # distillation result. Do NOT invent one — it would poison the store.
            raise RuntimeError(
                f"distill: model returned no parseable lessons from completion {content!r}"
            )
        return lessons

    def _complete(self, prompt: str) -> str:
        """POST the prompt and return the (stripped) completion text.

        Thinking is left ON and no grammar is sent (free-form lessons). Fail-Early: an
        empty completion (reasoning ran past ``max_tokens`` before answering) RAISES.
        """
        payload = {
            "messages": [{"role": "user", "content": prompt}],
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
        content: str = (data["choices"][0]["message"]["content"] or "").strip()
        if not content:
            raise RuntimeError(
                "distill: LLM returned empty content — reasoning likely ran past "
                "max_tokens before emitting the lessons"
            )
        return content


# --- outcome + swing rendering ----------------------------------------------------


def _side_name(side: str) -> str:
    if side == WHITE:
        return "White"
    if side == BLACK:
        return "Black"
    raise ValueError(f"unknown side {side!r}")


def _outcome_line(record: GameRecord, side: str) -> str:
    """A one-line result statement from ``side``'s perspective."""
    name = _side_name(side)
    if record.winner in (WHITE, BLACK):
        verb = "WON" if record.winner == side else "LOST"
        return f"Result: {name} {verb} by {record.reason}."
    if record.winner is None and record.reason in _DRAW_REASONS:
        return f"Result: the game was a draw ({record.reason})."
    # max-plies / any other non-terminal reason: an unfinished game, reported honestly.
    return f"Result: the game was unfinished ({record.reason})."


def _render_swings(record: GameRecord, swung: list[Swing], board_start: Board) -> str:
    """Render each swung move as ``[swing, tag] Side played <uci> in:`` + a board diagram.

    Replays the game's moves over a copy of ``board_start`` so each swung move's PRE-move
    position can be drawn (the record itself stores only per-ply evals, not boards). The
    swung moves are keyed by ``ply_index``; a ply index outside the game's move list is a
    corrupt target and RAISES (Fail-Early).
    """
    board = board_start.copy()
    pre_move_boards: dict[int, str] = {}
    targets = {s.ply_index for s in swung}
    for ply, uci in enumerate(record.moves):
        if ply in targets:
            pre_move_boards[ply] = _render_board(board)
        board.push(parse_move(board, uci))
    blocks: list[str] = []
    for s in sorted(swung, key=lambda x: x.ply_index):
        if s.ply_index not in pre_move_boards:
            raise ValueError(
                f"_render_swings: swung move at ply {s.ply_index} is outside the game's "
                f"{len(record.moves)} recorded moves — a corrupt distillation target"
            )
        tag = "MISTAKE" if s.swing < 0 else ("STRONG" if s.swing > 0 else "NEUTRAL")
        header = f"[swing {s.swing:+.2f}, {tag}] {_side_name(s.side)} played {s.move_uci} in:"
        blocks.append(header + "\n" + pre_move_boards[s.ply_index])
    return "\n\n".join(blocks)


def _render_board(board: Board) -> str:
    """A plain ASCII rendering, rank 6 at the top down to rank 1 (mirrors driver)."""
    lines = []
    for rank in range(BOARD_SIZE - 1, -1, -1):
        row = " ".join(board.squares[square(f, rank)] for f in range(BOARD_SIZE))
        lines.append(f"{rank + 1}  {row}")
    lines.append("   " + " ".join("abcdef"[:BOARD_SIZE]))
    return "\n".join(lines)


# --- lesson parsing --------------------------------------------------------------

# Strip a leading list marker: "1." / "1)" / "-" / "*" / "•" (with optional trailing space).
_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")


def _parse_lessons(content: str) -> list[str]:
    """Parse a free-form numbered/bulleted list into a list of lesson strings.

    Each non-blank line has its list marker stripped and surrounding whitespace trimmed;
    blank lines are dropped. A completion with no non-blank lines yields an empty list
    (the caller treats that as a Fail-Early error). This never fabricates a lesson.
    """
    lessons: list[str] = []
    for raw in content.splitlines():
        stripped = _MARKER.sub("", raw).strip()
        if stripped:
            lessons.append(stripped)
    return lessons
