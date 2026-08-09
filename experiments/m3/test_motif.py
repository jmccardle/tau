"""Tests for the M3 position->motif extractor (experiments/m3/motif.py).

Reference: docs/M3-DESIGN.md §4.6 (the retrieval key). These tests never contact
a real server — the LLM call is exercised against a FAKE httpx transport (a
canned chat-completion JSON), exactly as test_driver.py does for `LLMAgent`.

Beyond the mechanical contract (the descriptor carries the completion; an empty
completion Fails-Early), one test asserts the §4.6 ANTI-GOAL directly: the prompt
must render the position but must NOT ship a hand-authored enumerated motif
taxonomy — the vocabulary has to be the model's own.
"""

from __future__ import annotations

import json

import httpx
import pytest

from los_alamos import WHITE, Board
from motif import MotifDescriptor, MotifExtractor, dump_descriptors


def _fake_client(content: str, captured: dict[str, object] | None = None) -> httpx.Client:
    """An httpx.Client whose every POST returns a canned chat-completion whose
    message content is `content`. Optionally records the request payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- describe: the descriptor carries the emergent text ---------------------


def test_describe_carries_completion_text() -> None:
    board = Board()
    canned = (
        "White has a lead in central space and Black's queen is boxed in; the "
        "pressure is on the c-file."
    )
    extractor = MotifExtractor("http://test.invalid", client=_fake_client(canned))
    descriptor = extractor.describe(board)
    assert isinstance(descriptor, MotifDescriptor)
    assert descriptor.text == canned
    assert descriptor.position_key == board.position_key()


def test_describe_strips_whitespace() -> None:
    board = Board()
    extractor = MotifExtractor("http://test.invalid", client=_fake_client("  cramped king\n"))
    assert extractor.describe(board).text == "cramped king"


def test_describe_raises_on_empty_content() -> None:
    # Fail-Early: an empty completion (reasoning overran max_tokens) is not a key.
    board = Board()
    extractor = MotifExtractor("http://test.invalid", client=_fake_client(""))
    with pytest.raises(RuntimeError):
        extractor.describe(board)


# --- The prompt: renders the position, ships NO hand-authored taxonomy ------


def test_prompt_contains_rendered_position() -> None:
    board = Board()
    captured: dict[str, object] = {}
    extractor = MotifExtractor("http://test.invalid", client=_fake_client("ok", captured))
    extractor.describe(board)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    prompt = payload["messages"][0]["content"]
    # The ASCII rendering of the position is present (rank labels + the initial
    # back-rank pieces), so the model is keying off the actual board.
    assert "R N Q K N R" in prompt
    assert "a b c d e f" in prompt
    # No grammar is sent: this is free-form analysis, not a constrained move.
    assert "grammar" not in payload


def test_prompt_does_not_ship_a_motif_taxonomy() -> None:
    # The §4.6 anti-goal, asserted mechanically: the prompt must not hand the
    # model a fixed menu of motif names to choose from — the vocabulary is the
    # model's. Guard against both an enumerated "choose from: ..." list and the
    # specific motif NAMES a taxonomy would smuggle in.
    board = Board()
    captured: dict[str, object] = {}
    extractor = MotifExtractor("http://test.invalid", client=_fake_client("ok", captured))
    extractor.describe(board)

    payload = captured["payload"]
    assert isinstance(payload, dict)
    prompt = payload["messages"][0]["content"].lower()

    assert "choose from" not in prompt
    assert "select from" not in prompt
    # Hand-authored motif names must not be pre-supplied.
    for motif_name in ("fork", "pin", "skewer", "discovered attack", "zugzwang"):
        assert motif_name not in prompt


# --- dump_descriptors: one descriptor per input position --------------------


def test_dump_descriptors_one_per_position() -> None:
    boards = [
        Board(),
        Board.from_piece_map({"a1": "K", "c3": "Q", "f6": "k"}, side=WHITE),
        Board.from_piece_map({"d1": "K", "d6": "k", "b2": "R"}, side=WHITE),
    ]
    extractor = MotifExtractor("http://test.invalid", client=_fake_client("a salient feature"))
    results = dump_descriptors(boards, extractor)

    assert len(results) == len(boards)
    for board, (key, descriptor) in zip(boards, results):
        assert key == board.position_key()
        assert isinstance(descriptor, MotifDescriptor)
        assert descriptor.text == "a salient feature"
        assert descriptor.position_key == board.position_key()


def test_dump_descriptors_preserves_distinct_positions() -> None:
    # Distinct positions must yield distinct provenance keys (the review pass
    # relies on being able to trace a descriptor back to its position).
    boards = [
        Board(),
        Board.from_piece_map({"a1": "K", "f6": "k"}, side=WHITE),
    ]
    extractor = MotifExtractor("http://test.invalid", client=_fake_client("desc"))
    keys = [key for key, _ in dump_descriptors(boards, extractor)]
    assert keys[0] != keys[1]
    assert boards[0].side == WHITE  # sanity: same side-to-move, different placement
