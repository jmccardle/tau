"""Output limits: read and grep keep the head, bash keeps the tail, the whole goes to a file."""

import os
import tempfile
from pathlib import Path

import pytest
from calls import call, err, ok, parse_truncated

from tau_next import BashTool, GrepTool, ReadTool, WriteTool

LIMIT = 200


def numbered(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


def test_read_keeps_the_head_and_the_file_holds_everything(tmp_path: Path, overflow: Path) -> None:
    text = numbered(1000)
    (tmp_path / "big.txt").write_text(text)
    tool = ReadTool(tmp_path, max_output=LIMIT, overflow_dir=overflow)
    answer = parse_truncated(ok(call(tool, path="big.txt")))
    assert answer.where == "after"
    assert text.startswith(answer.kept)
    assert answer.kept.startswith("line 1\nline 2\n")
    assert len(answer.kept.encode()) <= LIMIT
    assert answer.omitted == 1000 - answer.kept.count("\n") - 1  # kept has no final newline
    assert answer.full == text.encode()
    assert answer.size == len(text.encode())
    assert answer.path.parent == overflow
    assert len(answer.path.stem) == 8
    assert answer.path.suffix == ".out"
    assert answer.path.stem.isalnum()


def test_bash_keeps_the_tail_and_the_file_holds_everything(tmp_path: Path, overflow: Path) -> None:
    tool = BashTool(tmp_path, max_output=LIMIT, overflow_dir=overflow)
    answer = parse_truncated(ok(call(tool, command="seq 1 2000")))
    full = "".join(f"{i}\n" for i in range(1, 2001))
    assert answer.where == "before"
    assert answer.kept.endswith("1999\n2000\n")
    assert full.endswith(answer.kept)
    assert len(answer.kept.encode()) <= LIMIT
    assert answer.omitted == 2000 - answer.kept.count("\n")
    assert answer.full == full.encode()
    # the kept text starts on a line boundary
    assert full[len(full) - len(answer.kept) - 1] == "\n"


def test_bash_failure_keeps_the_end_of_the_output_and_the_exit_code(
    tmp_path: Path, overflow: Path
) -> None:
    tool = BashTool(tmp_path, max_output=LIMIT, overflow_dir=overflow)
    text = err(call(tool, command="seq 1 2000; echo 'fatal: the end' >&2; exit 4"))
    assert text.endswith("fatal: the end\n\ncommand exited with code 4")
    answer = parse_truncated(text)
    assert answer.full.endswith(b"fatal: the end\n")


def test_grep_keeps_the_head_and_the_file_holds_everything(tmp_path: Path, overflow: Path) -> None:
    (tmp_path / "a.txt").write_text(numbered(500))
    tool = GrepTool(tmp_path, max_output=LIMIT, overflow_dir=overflow)
    answer = parse_truncated(ok(call(tool, pattern="line")))
    full = "".join(f"a.txt:{i}:line {i}\n" for i in range(1, 501))
    assert answer.where == "after"
    assert answer.kept.startswith("a.txt:1:line 1\na.txt:2:line 2")
    assert len(answer.kept.encode()) <= LIMIT
    assert answer.omitted == 500 - answer.kept.count("\n") - 1
    assert answer.full == full.removesuffix("\n").encode()


def test_the_note_text(tmp_path: Path, overflow: Path) -> None:
    (tmp_path / "f.txt").write_text("aaaa\nbbbb\ncccc\ndddd\n")
    head = ok(call(ReadTool(tmp_path, max_output=10, overflow_dir=overflow), path="f.txt"))
    path = parse_truncated(head).path
    assert head == (
        "aaaa\nbbbb\n[truncated: 2 lines omitted after these; "
        f"full output (20 bytes) is in {path}]\n"
        "[showing lines 1-2; offset=3 continues]"
    )
    tail = ok(call(BashTool(tmp_path, max_output=10, overflow_dir=overflow), command="cat f.txt"))
    path = parse_truncated(tail).path
    assert tail == (
        f"[truncated: 2 lines omitted before these; full output (20 bytes) is in {path}]\n"
        "cccc\ndddd\n"
    )


def test_output_within_the_limit_is_untouched_and_writes_no_file(
    tmp_path: Path, overflow: Path
) -> None:
    (tmp_path / "f.txt").write_text("x" * 200)
    out = call(ReadTool(tmp_path, max_output=200, overflow_dir=overflow), path="f.txt")
    assert ok(out) == "x" * 200
    assert not overflow.exists()
    bash = BashTool(tmp_path, max_output=200, overflow_dir=overflow)
    assert ok(call(bash, command="printf 'y%.0s' $(seq 200)")) == "y" * 200
    assert not overflow.exists()
    assert "truncated" in ok(call(bash, command="printf 'y%.0s' $(seq 201)"))


@pytest.mark.parametrize("limit", [7, 8, 9, 10, 11, 12, 13, 25, 50])
def test_a_cut_never_splits_a_utf8_character(tmp_path: Path, overflow: Path, limit: int) -> None:
    text = "日本語é\n" * 40
    (tmp_path / "u.txt").write_text(text)
    read = ReadTool(tmp_path, max_output=limit, overflow_dir=overflow)
    bash = BashTool(tmp_path, max_output=limit, overflow_dir=overflow)
    for outcome in (call(read, path="u.txt"), call(bash, command="cat u.txt")):
        answer = parse_truncated(ok(outcome))
        assert "�" not in answer.kept
        if answer.cut:  # no whole line fits: part of one is kept, from a character boundary
            assert limit < len("日本語é\n".encode())
            assert answer.kept.strip("\n") in text
        else:
            kept_lines = answer.kept.strip("\n").split("\n")
            assert kept_lines == ["日本語é"] * len(kept_lines)
        assert len(answer.kept.encode()) <= limit
        assert answer.full == text.encode()


def test_one_line_longer_than_the_limit_is_cut_on_a_character(
    tmp_path: Path, overflow: Path
) -> None:
    (tmp_path / "long.txt").write_text("é" * 100 + "\n")
    head = parse_truncated(
        ok(call(ReadTool(tmp_path, max_output=15, overflow_dir=overflow), path="long.txt"))
    )
    assert head.cut
    assert head.kept == "é" * 7
    tail = parse_truncated(
        ok(call(BashTool(tmp_path, max_output=15, overflow_dir=overflow), command="cat long.txt"))
    )
    assert tail.cut
    assert tail.kept.rstrip("\n") == "é" * 7


def test_the_default_limit_is_10_kb_and_the_default_directory_is_the_temp_dir() -> None:
    with tempfile.TemporaryDirectory() as work:
        Path(work, "big.txt").write_text("0123456789\n" * 3000)
        answer = parse_truncated(ok(call(ReadTool(work), path="big.txt")))
    try:
        assert answer.path.parent == Path(tempfile.gettempdir()) / f"tau-{os.geteuid()}"
        assert 9_000 < len(answer.kept.encode()) <= 10_000
        assert answer.size == 33_000
    finally:
        answer.path.unlink()


def test_the_overflow_file_cannot_be_written_is_an_error_not_a_silent_cut(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.txt").write_text("x" * 100)
    unwritable = tmp_path / "blocker" / "tau"
    read = ReadTool(tmp_path, max_output=10, overflow_dir=unwritable)
    grep = GrepTool(tmp_path, max_output=10, overflow_dir=unwritable)
    bash = BashTool(tmp_path, max_output=10, overflow_dir=unwritable)
    (tmp_path / "blocker").write_text("a file where the directory should be")
    assert "could not be created" in err(call(read, path="f.txt"))
    assert "could not be created" in err(call(grep, pattern="x"))
    assert "could not be created" in err(call(bash, command="seq 1 100"))


def test_the_write_tool_has_no_limit_to_exceed(tmp_path: Path) -> None:
    out = ok(call(WriteTool(tmp_path), path="f.txt", content="x" * 100_000))
    assert out == "Wrote 1 line (100000 bytes) to f.txt"
