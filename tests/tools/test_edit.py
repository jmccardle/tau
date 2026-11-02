"""`edit`: one exact search and replace in one file, answered with a unified diff."""

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest
from calls import call, err, ok, parse_truncated

from tau import EditTool, Outcome


def lines(n: int, mark: str = "x") -> str:
    return "".join(f"{mark}{i}\n" for i in range(1, n + 1))


def test_replaces_the_text_and_answers_a_unified_diff(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    out = ok(call(EditTool(tmp_path), path="a.txt", old_string="two", new_string="TWO"))
    assert out == (
        "Replaced 1 occurrence in a.txt\n"
        "--- a.txt\n"
        "+++ a.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three"
    )
    assert (tmp_path / "a.txt").read_text() == "one\nTWO\nthree\n"


def test_a_hunk_has_three_lines_of_context(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(lines(20))
    out = ok(call(EditTool(tmp_path), path="a.txt", old_string="x10\n", new_string="ten\n"))
    assert out.split("\n", 3)[3] == ("@@ -7,7 +7,7 @@\n x7\n x8\n x9\n-x10\n+ten\n x11\n x12\n x13")


def test_a_deletion_and_an_insertion_count_their_lines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    tool = EditTool(tmp_path)
    gone = ok(call(tool, path="a.txt", old_string="two\n", new_string=""))
    assert gone.endswith("@@ -1,3 +1,2 @@\n one\n-two\n three")
    added = ok(call(tool, path="a.txt", old_string="one\n", new_string="one\ntwo\n"))
    assert added.endswith("@@ -1,2 +1,3 @@\n one\n+two\n three")
    assert (tmp_path / "a.txt").read_text() == "one\ntwo\nthree\n"


def test_a_multi_line_replacement_shows_only_the_lines_that_changed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("head\nsame\nold\nsame2\ntail\n")
    out = ok(
        call(EditTool(tmp_path), path="a.txt", old_string="same\nold\n", new_string="same\nnew\n")
    )
    assert out.endswith("@@ -1,5 +1,5 @@\n head\n same\n-old\n+new\n same2\n tail")


def test_two_occurrences_on_one_line_are_one_change(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a a\nb\n")
    out = ok(
        call(EditTool(tmp_path), path="a.txt", old_string="a", new_string="z", replace_all=True)
    )
    assert out.startswith("Replaced 2 occurrences in a.txt\n")
    assert out.endswith("@@ -1,2 +1,2 @@\n-a a\n+z z\n b")


def test_replace_all_replaces_every_occurrence_in_separate_hunks_when_far_apart(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("hit\n" + lines(20) + "hit\n")
    out = ok(
        call(EditTool(tmp_path), path="a.txt", old_string="hit", new_string="HIT", replace_all=True)
    )
    assert out.startswith("Replaced 2 occurrences in a.txt\n")
    assert out.count("\n@@ ") == 2
    first = "@@ -1,4 +1,4 @@\n-hit\n+HIT\n x1\n x2\n x3\n"
    second = "@@ -19,4 +19,4 @@\n x18\n x19\n x20\n-hit\n+HIT"
    assert first + second in out
    assert (tmp_path / "a.txt").read_text() == "HIT\n" + lines(20) + "HIT\n"


def test_nearby_occurrences_share_one_hunk(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit\nmid\nhit\n")
    out = ok(
        call(EditTool(tmp_path), path="a.txt", old_string="hit", new_string="HIT", replace_all=True)
    )
    assert out.count("\n@@ ") == 1
    assert out.endswith("@@ -1,3 +1,3 @@\n-hit\n+HIT\n mid\n-hit\n+HIT")


def test_a_missing_final_newline_is_kept_and_shown(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb")
    out = ok(call(EditTool(tmp_path), path="a.txt", old_string="b", new_string="c"))
    assert (tmp_path / "a.txt").read_bytes() == b"a\nc"
    assert out.endswith(
        "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file"
    )


def test_adding_the_final_newline_is_a_change(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"a\nb")
    out = ok(call(EditTool(tmp_path), path="a.txt", old_string="b", new_string="b\n"))
    assert (tmp_path / "a.txt").read_bytes() == b"a\nb\n"
    assert out.endswith("@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b")


def test_crlf_lines_stay_crlf_and_the_model_writes_plain_newlines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    out = ok(
        call(
            EditTool(tmp_path),
            path="a.txt",
            old_string="one\ntwo\n",
            new_string="uno\ndos\ntres\n",
        )
    )
    assert (tmp_path / "a.txt").read_bytes() == b"uno\r\ndos\r\ntres\r\nthree\r\n"
    assert out.endswith("@@ -1,3 +1,4 @@\n-one\n-two\n+uno\n+dos\n+tres\n three")


def test_a_crlf_file_also_matches_text_that_is_already_crlf(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    call(EditTool(tmp_path), path="a.txt", old_string="one\r\n", new_string="1\r\n")
    assert (tmp_path / "a.txt").read_bytes() == b"1\r\ntwo\r\n"


def test_a_file_with_mixed_endings_is_matched_literally(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\nthree\r\n")
    tool = EditTool(tmp_path)
    assert "not found" in err(call(tool, path="a.txt", old_string="one\ntwo", new_string="x"))
    ok(call(tool, path="a.txt", old_string="two\n", new_string="2\n"))
    assert (tmp_path / "a.txt").read_bytes() == b"one\r\n2\nthree\r\n"


def test_text_not_found_says_how_to_fix_it_and_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one\n")
    message = err(call(EditTool(tmp_path), path="a.txt", old_string="two", new_string="x"))
    assert message.startswith("old_string not found in a.txt")
    assert "exactly" in message
    assert (tmp_path / "a.txt").read_text() == "one\n"


def test_ambiguous_text_names_the_lines_and_the_two_ways_out(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit\nx\nx\nhit\nhit\n")
    message = err(call(EditTool(tmp_path), path="a.txt", old_string="hit", new_string="y"))
    assert message == (
        "old_string occurs 3 times in a.txt (lines 1, 4, 5): add the lines around the one to "
        "change to make it unique, or set replace_all to change every occurrence"
    )
    assert (tmp_path / "a.txt").read_text() == "hit\nx\nx\nhit\nhit\n"


def test_ambiguous_text_names_only_the_first_lines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit\n" * 9)
    message = err(call(EditTool(tmp_path), path="a.txt", old_string="hit", new_string="y"))
    assert "occurs 9 times in a.txt (lines 1, 2, 3, 4, 5, and 4 more)" in message


def test_matches_do_not_overlap(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("aaa\n")
    ok(call(EditTool(tmp_path), path="a.txt", old_string="aa", new_string="b"))
    assert (tmp_path / "a.txt").read_text() == "ba\n"


def test_a_file_that_is_not_utf8_is_refused_untouched(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"caf\xe9 x\n")
    message = err(call(EditTool(tmp_path), path="a.bin", old_string="x", new_string="y"))
    assert message.startswith("a.bin is not valid UTF-8 (the first bad byte is at offset 3)")
    assert (tmp_path / "a.bin").read_bytes() == b"caf\xe9 x\n"


def test_a_missing_file_points_to_write(tmp_path: Path) -> None:
    message = err(call(EditTool(tmp_path), path="nope.txt", old_string="a", new_string="b"))
    assert message == "file not found: nope.txt (edit changes an existing file; write creates one)"
    assert os.listdir(tmp_path) == []


def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    message = err(call(EditTool(tmp_path), path="d", old_string="a", new_string="b"))
    assert message == "d is a directory, not a file"


def test_bad_arguments_are_errors(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n")
    tool = EditTool(tmp_path)
    cases: list[tuple[dict[str, object], str]] = [
        ({"path": "", "old_string": "a", "new_string": "b"}, "`path` must not be empty"),
        ({"path": "a.txt", "old_string": "", "new_string": "b"}, "`old_string` must not be empty"),
        (
            {"path": "a.txt", "old_string": "a", "new_string": "a"},
            "`old_string` and `new_string` are the same",
        ),
        ({"path": "a.txt", "old_string": "a"}, "missing field `new_string`"),
        ({"path": "a.txt", "old_string": "a", "new_string": "b", "all": True}, "unknown field"),
    ]
    for args, message in cases:
        assert message in err(call(tool, **args)), args
    assert (tmp_path / "a.txt").read_text() == "a\n"


def test_the_write_is_atomic_and_keeps_the_mode(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    call(EditTool(tmp_path), path="run.sh", old_string="old", new_string="new")
    assert script.read_text() == "echo new\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert os.listdir(tmp_path) == ["run.sh"]


def test_a_symlink_stays_a_link_and_its_target_changes(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("old\n")
    (tmp_path / "link.txt").symlink_to("real.txt")
    call(EditTool(tmp_path), path="link.txt", old_string="old", new_string="new")
    assert (tmp_path / "link.txt").is_symlink()
    assert (tmp_path / "real.txt").read_text() == "new\n"


def test_relative_paths_are_a_default_not_a_sandbox(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "outside.txt").write_text("old\n")
    tool = EditTool(tmp_path / "work")
    ok(call(tool, path="../outside.txt", old_string="old", new_string="new"))
    ok(call(tool, path=str(tmp_path / "outside.txt"), old_string="new", new_string="newer"))
    assert (tmp_path / "outside.txt").read_text() == "newer\n"


def test_a_long_diff_keeps_the_head_and_the_file_holds_the_whole(
    tmp_path: Path, overflow: Path
) -> None:
    (tmp_path / "a.txt").write_text(lines(300))
    tool = EditTool(tmp_path, max_output=200, overflow_dir=overflow)
    out = ok(call(tool, path="a.txt", old_string="x", new_string="y", replace_all=True))
    answer = parse_truncated(out)
    assert answer.kept.startswith("Replaced 300 occurrences in a.txt\n--- a.txt\n+++ a.txt\n@@ ")
    assert len(answer.kept.encode()) <= 200
    assert answer.full.decode().count("\n+y") == 300
    assert (tmp_path / "a.txt").read_text() == lines(300, "y")


def test_a_missing_working_directory_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        EditTool(tmp_path / "nope")


def test_two_edits_of_one_file_in_one_step_both_land(tmp_path: Path) -> None:
    tool = EditTool(tmp_path)
    body = "pad line\n" * 2000

    async def both() -> None:
        first, second = await asyncio.gather(
            tool.call(json.dumps({"path": "f.txt", "old_string": "AAA", "new_string": "aaa"})),
            tool.call(json.dumps({"path": "f.txt", "old_string": "BBB", "new_string": "bbb"})),
        )
        assert isinstance(first, Outcome.Ok), first
        assert isinstance(second, Outcome.Ok), second

    for _ in range(20):
        (tmp_path / "f.txt").write_text(f"AAA\n{body}BBB\n")
        asyncio.run(both())
        assert (tmp_path / "f.txt").read_text() == f"aaa\n{body}bbb\n"


def test_a_replacement_that_joins_the_next_line_shows_both_lines_changed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nc\nd\n")
    out = ok(call(EditTool(tmp_path), path="a.txt", old_string="c\n", new_string="X"))
    assert out.endswith("@@ -1,3 +1,2 @@\n a\n-c\n-d\n+Xd")
    assert (tmp_path / "a.txt").read_text() == "a\nXd\n"
