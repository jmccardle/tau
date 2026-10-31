"""`read`: arguments, errors, and line selection."""

import asyncio
from pathlib import Path

from calls import call, err

from tau import Outcome, ReadTool


def test_reads_a_file_verbatim_relative_to_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    assert call(ReadTool(tmp_path), path="a.txt") == Outcome.Ok("one\ntwo\n")
    assert call(ReadTool(), path=str(tmp_path / "a.txt")) == Outcome.Ok("one\ntwo\n")


def test_non_ascii_text_and_an_empty_file(tmp_path: Path) -> None:
    (tmp_path / "u.txt").write_text("日本語 é\n")
    (tmp_path / "empty.txt").write_text("")
    assert call(ReadTool(tmp_path), path="u.txt") == Outcome.Ok("日本語 é\n")
    assert call(ReadTool(tmp_path), path="empty.txt") == Outcome.Ok("")


def test_offset_and_limit_select_lines_and_say_how_to_continue(tmp_path: Path) -> None:
    (tmp_path / "n.txt").write_text("".join(f"{i}\n" for i in range(1, 11)))
    tool = ReadTool(tmp_path)
    middle = call(tool, path="n.txt", offset=3, limit=2)
    assert middle == Outcome.Ok("3\n4\n\n[showing lines 3-4; offset=5 continues]")
    assert call(tool, path="n.txt", offset=9) == Outcome.Ok("9\n10\n")
    assert call(tool, path="n.txt", offset=9, limit=2) == Outcome.Ok("9\n10\n")


def test_errors_are_outcomes_the_model_can_read(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (tmp_path / "one.txt").write_text("x\n")
    tool = ReadTool(tmp_path)
    assert "file not found: missing.txt" in err(call(tool, path="missing.txt"))
    assert "is a directory" in err(call(tool, path="dir"))
    assert "binary file" in err(call(tool, path="bin"))
    cases: list[tuple[dict[str, object], str]] = [
        ({"path": "one.txt", "offset": 0}, "`offset` must be at least 1"),
        ({"path": "one.txt", "limit": -3}, "`limit` must be at least 1"),
        ({"path": "one.txt", "offset": 5}, "past the end"),
        ({}, "missing field `path`"),
        ({"path": "one.txt", "extra": 1}, "unknown field `extra`"),
        ({"path": 3}, "invalid arguments"),
    ]
    for args, message in cases:
        assert message in err(call(tool, **args)), args


def test_arguments_that_are_not_json_are_an_error_not_a_raise(tmp_path: Path) -> None:
    outcome = asyncio.run(ReadTool(tmp_path).call("{not json"))
    assert err(outcome).startswith("invalid arguments")
