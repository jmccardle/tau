"""`write`: whole files, atomically, with parents."""

import os
import stat
import sys
from pathlib import Path

import pytest
from calls import call, err, put, text_of

from tau import Outcome, WriteTool


def test_writes_creating_parents_and_counts_lines_and_bytes(tmp_path: Path) -> None:
    out = call(WriteTool(tmp_path), path="a/b/c.txt", content="é\nx\n")
    assert out == Outcome.Ok("Wrote 2 lines (5 bytes) to a/b/c.txt")
    assert text_of(tmp_path / "a/b/c.txt") == "é\nx\n"


def test_replacing_leaves_no_temporary_file_and_keeps_the_mode(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    put(script, "old")
    if sys.platform != "win32":  # mode bits: Windows has none beyond read-only
        script.chmod(0o755)
    call(WriteTool(tmp_path), path="run.sh", content="new")
    assert text_of(script) == "new"
    if sys.platform != "win32":
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert os.listdir(tmp_path) == ["run.sh"]


def test_writes_through_a_symlink(tmp_path: Path) -> None:
    put((tmp_path / "real.txt"), "old")
    (tmp_path / "link.txt").symlink_to("real.txt")
    call(WriteTool(tmp_path), path="link.txt", content="new")
    assert (tmp_path / "link.txt").is_symlink()
    assert text_of(tmp_path / "real.txt") == "new"


def test_errors_are_outcomes(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    put((tmp_path / "file"), "x")
    tool = WriteTool(tmp_path)
    cases: list[tuple[dict[str, object], str]] = [
        ({"path": "dir", "content": ""}, "is a directory"),
        ({"path": "file/under", "content": ""}, "cannot write file/under"),
        ({"path": "", "content": ""}, "must not be empty"),
        ({"path": "x"}, "missing field `content`"),
    ]
    for args, message in cases:
        assert message in err(call(tool, **args)), args


def test_a_missing_working_directory_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        WriteTool(tmp_path / "nope")
