"""The Gemini reader's half of the `ReplyReader` contract, and the parts it refuses."""

import json
from collections.abc import Mapping

import pytest
from harness import scenario
from wire import GEMINI, collect, gemini_chunk, hello_walk

from tau_next import Finish, Part, Stop


def sse(chunk: Mapping[str, object]) -> str:
    return f"data: {json.dumps(chunk)}\r\n\r\n"


@scenario
async def test_parts_a_chunk_finished_before_it_failed_are_kept() -> None:
    first = {"functionCall": {"name": "read", "args": {"p": 1}, "id": "c1"}}
    text = gemini_chunk([first, {"text": "x", "thought": "yes"}], "STOP")
    items, error = await collect(GEMINI.serving(text), hello_walk())
    parts = [i for i in items if isinstance(i, Part)]
    assert [(p.id, p.tool, p.args_raw) for p in parts if isinstance(p, Part.Call)] == [
        ("c1", "read", '{"p": 1}')
    ]
    assert error is not None
    assert "thought" in str(error)


@scenario
async def test_thought_must_be_a_boolean() -> None:
    text = gemini_chunk([{"text": "x", "thought": "true"}], "STOP")
    items, error = await collect(GEMINI.serving(text), hello_walk())
    assert items == []
    assert error is not None
    assert "thought is not a boolean" in str(error)


@scenario
async def test_content_parts_must_be_a_list() -> None:
    chunk = {"candidates": [{"content": {"parts": {"text": "x"}}, "finishReason": "STOP"}]}
    _, error = await collect(GEMINI.serving(sse(chunk)), hello_walk())
    assert error is not None
    assert "not a list" in str(error)


@scenario
async def test_a_part_with_two_data_keys_is_refused() -> None:
    both = {"text": "x", "functionCall": {"name": "read", "args": {}}}
    _, error = await collect(GEMINI.serving(gemini_chunk([both], "STOP")), hello_walk())
    assert error is not None
    assert "more than one" in str(error)


@pytest.mark.parametrize(
    "key", ["partMetadata", "mediaResolution", "mediaProcessing", "speechMetadata"]
)
@scenario
async def test_metadata_part_keys_are_allowed_and_ignored(key: str) -> None:
    text = gemini_chunk([{"text": "hi", key: {"x": 1}}], "STOP")
    items, error = await collect(GEMINI.serving(text), hello_walk())
    assert error is None
    assert [i for i in items if isinstance(i, Part | Finish)] == [
        Part.Text("hi"),
        Finish(Stop.End, "STOP"),
    ]


@scenario
async def test_candidates_in_a_blocked_chunk_are_not_turned_into_parts() -> None:
    chunk = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "candidates": [{"content": {"parts": [{"text": "leak"}]}}],
    }
    items, error = await collect(GEMINI.serving(sse(chunk)), hello_walk())
    assert error is None
    assert [type(i) for i in items] == [Finish]
    assert (items[0].stop, items[0].reason) == (Stop.Refused, "SAFETY")  # type: ignore[union-attr]
