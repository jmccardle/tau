"""Annotations as strings (`from __future__ import annotations`) resolve like real ones."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from harness import called, offered, scenario

from tau_next import Outcome


def search(
    query: str,
    kinds: list[Literal["a", "b"]] | None = None,
    n: Optional[int] = None,  # noqa: UP045
) -> str:
    """Search."""
    return f"{query} {kinds} {n}"


@scenario
async def test_string_annotations_make_the_schema(tmp_path: Path) -> None:
    (found,) = await offered(tmp_path, search)
    assert found.parameters["properties"] == {
        "query": {"type": "string"},
        "kinds": {
            "anyOf": [
                {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
                {"type": "null"},
            ]
        },
        "n": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    }
    assert found.parameters["required"] == ["query"]


@scenario
async def test_string_annotations_are_checked_at_call(tmp_path: Path) -> None:
    assert await called(tmp_path, search, '{"query": "q", "n": 2}') == Outcome.Ok("q None 2")
