"""Position -> motif extractor: the M3 RETRIEVAL KEY (§4.6).

Reference: docs/M3-DESIGN.md §4.6 (the retrieval key — position->query and what a
strategy is keyed on). This module turns a `Board` into a *tactical abstraction*
of the position that becomes the key a strategy is retrieved on. The design has a
strong stance, which this module enforces mechanically:

    The motif vocabulary is MODEL-GENERATED, not hand-authored, and REVIEW-GATED.

Neither the owner nor the harness is qualified to name the strategic motifs of a
game with essentially no prior play; a hand-coded feature set would presuppose
the very strategy that should emerge and is as likely to poison the strategy
space as to guide it (§4.6). So the extractor asks the model to describe the
position's tactical situation in its OWN words and does NOT hand it a fixed list
of motif names to choose from. The emergent description IS the key (it is what
gets embedded); the emergent vocabulary is itself a documented finding. We
review it across a sample (`dump_descriptors`) BEFORE intervening, and then only
on a specific pathology (e.g. pure-surface "knight on c3" descriptors) and only
by a prompt nudge — never by imposing a taxonomy.

The LLM call mirrors experiments/m3/driver.py's `LLMAgent` (httpx POST to a
chat/completions endpoint, thinking left ON, `data["choices"][0]["message"]
["content"]` extraction, an optional injected `client` for testing) but is
FREE-FORM text: a description, not a grammar-constrained move.

Fail-Early (repo rule): an empty/truncated completion is NOT a key. `describe`
RAISES rather than fabricating a descriptor — a poisoned key is worse than a
missing one.

Depends only on the stdlib + httpx. This module does NOT manage or contact an
inference server at import time; an endpoint is always supplied by the caller.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import httpx

from driver import EngineAgent, _render_board
from los_alamos import WHITE, Board

# --- The descriptor ---------------------------------------------------------


@dataclass(frozen=True)
class MotifDescriptor:
    """The retrieval key for one position (§4.6).

    `text` is the model's own free-form description of the tactical situation —
    the AUTHORITATIVE emergent wording that gets embedded as the key. It is kept
    verbatim; no structured parse is allowed to discard or paraphrase it.
    `position_key` is the engine's placement+side identity (`Board.position_key`)
    carried for provenance, so a dumped descriptor can be traced back to the
    exact position it describes during the §4.6 review pass.
    """

    text: str
    position_key: str


# --- Prompt framing (review this before it hits the real model) -------------

# The game rules, so the model can reason about the position — but with NO
# instruction to emit a move (this is analysis, not play) and, deliberately, NO
# enumerated menu of motif names. The vocabulary must be the model's own (§4.6).
_SYSTEM_PREFACE = (
    "You are analysing a position in Los Alamos chess: a 6x6 variant on files "
    "a-f, ranks 1-6. There are NO bishops. The queen keeps full "
    "orthogonal+diagonal movement; rooks move orthogonally, knights the L, kings "
    "one square. Pawns move forward exactly ONE square (no two-square first "
    "move), capture diagonally, have no en passant, and promote on the last rank "
    "to Q/R/N (no bishop). There is no castling."
)

# The task. BARED-DOWN after the first emergent-vocabulary review (2026-07-17):
# an earlier version named categories of salience (threats / weaknesses /
# imbalances / initiative), and the "where the initiative lies" clause in
# particular forced the model to declare a one-sided verdict even in dead-equal
# openings — producing grandiose, boilerplate-heavy descriptions that would swamp
# the discriminating tactical signal in the embedded key. This version keeps only
# the anti-placement-dump guard (the §4.6 pathology to avoid) and a brevity nudge,
# and lets the vocabulary — and what counts as "important" — stay entirely the
# model's own. It still supplies NO motif names (no "fork / pin / skewer ..."):
# naming the motifs is exactly what we want to observe the model do unaided.
_TASK_INSTRUCTION = (
    "Describe what matters tactically in this position, briefly and in your own "
    "words, surfacing only the few most important features. Do NOT restate where "
    "each piece sits; a square-by-square list of placements is not what is wanted."
)


class MotifExtractor:
    """Turns a `Board` into a `MotifDescriptor` via a free-form LLM call (§4.6).

    Each call renders the position (reusing driver's ASCII board), asks the model
    to describe the tactical situation in its own vocabulary, POSTs a
    chat/completions request with thinking left ON and NO grammar (the output is
    a description, not a constrained move), and extracts the completion text.

    Fail-Early: an empty completion (reasoning ran past `max_tokens` before the
    description was emitted) RAISES — a truncated/empty description is not a
    retrieval key. An optional `client` may be injected for testing; otherwise a
    real `httpx.Client` is created per call and closed.
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

    def _build_prompt(self, board: Board) -> str:
        mover = "White" if board.side == WHITE else "Black"
        parts = [
            _SYSTEM_PREFACE,
            f"\nIt is {mover} to move.",
            "Board (uppercase = White, lowercase = Black, '.' = empty):",
            _render_board(board),
            "",
            _TASK_INSTRUCTION,
        ]
        return "\n".join(parts)

    def describe(self, board: Board) -> MotifDescriptor:
        payload = {
            "messages": [{"role": "user", "content": self._build_prompt(board)}],
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
        text = (data["choices"][0]["message"]["content"] or "").strip()
        if not text:
            # Fail-Early: a truncated-before-answer completion is not a key. Do
            # NOT fabricate a descriptor — a poisoned key corrupts every
            # retrieval that later matches on it.
            raise RuntimeError(
                "LLM returned empty content — reasoning likely ran past max_tokens "
                "before emitting a position description"
            )
        return MotifDescriptor(text=text, position_key=board.position_key())


# --- Review helper: dump descriptors across a sample (§4.6 review step) ------


def dump_descriptors(
    boards: Iterable[Board], extractor: MotifExtractor
) -> list[tuple[str, MotifDescriptor]]:
    """Run `extractor` over a sample of positions and collect the descriptors.

    Returns one `(position_key, MotifDescriptor)` per input board, in input
    order. This is the raw material for the §4.6 "review before intervene" pass:
    the owner eyeballs the emergent vocabulary across many positions in one go
    and decides whether any prompt nudge is warranted (never a taxonomy).
    """
    return [(board.position_key(), extractor.describe(board)) for board in boards]


# --- Sample generator + script entrypoint -----------------------------------


def _sample_positions(num_plies: int, depth: int) -> list[Board]:
    """A deterministic sample of real positions for review.

    Plays a single engine-vs-engine game (fully local, no server) and snapshots
    a copy of the board after each ply, so the review pass sees positions that
    actually arise in play rather than hand-picked ones. Stops early if the game
    ends before `num_plies`.
    """
    white = EngineAgent(depth)
    black = EngineAgent(depth)
    board = Board()
    positions: list[Board] = []
    for _ in range(num_plies):
        if board.is_game_over():
            break
        agent = white if board.side == WHITE else black
        board.push(agent.choose(board))
        positions.append(board.copy())
    return positions


def main(argv: Sequence[str] | None = None) -> int:
    """Print each sampled position and its model-generated descriptor.

    The endpoint is a required argument; nothing contacts a server unless this is
    run explicitly. Use it to eyeball the emergent motif vocabulary (§4.6).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="OpenAI-compatible chat/completions URL")
    parser.add_argument("--plies", type=int, default=12, help="positions to sample")
    parser.add_argument("--depth", type=int, default=2, help="engine sampler depth")
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args(argv)

    boards = _sample_positions(args.plies, args.depth)
    extractor = MotifExtractor(
        args.endpoint,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )
    descriptors = dump_descriptors(boards, extractor)
    for i, (board, (_key, descriptor)) in enumerate(zip(boards, descriptors)):
        print(f"=== position {i} ({descriptor.position_key}) ===")
        print(_render_board(board))
        print()
        print(descriptor.text)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
