"""Annotations as strings (`from __future__ import annotations`) resolve like real ones."""

from __future__ import annotations

from typing import Literal, Optional

from harness import scenario

from tau_next import Outcome, tool


@tool
def search(
    query: str,
    kinds: list[Literal["a", "b"]] | None = None,
    n: Optional[int] = None,  # noqa: UP045
) -> str:
    """Search."""
    return f"{query} {kinds} {n}"


def test_string_annotations_make_the_schema() -> None:
    assert search.parameters["properties"] == {
        "query": {"type": "string"},
        "kinds": {
            "anyOf": [
                {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
                {"type": "null"},
            ]
        },
        "n": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    }
    assert search.parameters["required"] == ["query"]


@scenario
async def test_string_annotations_are_checked_at_call() -> None:
    assert await search.call('{"query": "q", "n": 2}') == Outcome.Ok("q None 2")
