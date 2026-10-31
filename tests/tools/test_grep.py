"""`grep`: matching, ignore rules, options, and errors."""

from pathlib import Path

from calls import call, err, ok

from tau import GrepTool, Outcome


def test_matches_are_path_line_text_sorted_by_path(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("needle in sub\r\nnothing\n")
    (tmp_path / "b.txt").write_text("first\nthe needle\nneedle needle\n")
    out = call(GrepTool(tmp_path), pattern="needle")
    assert out == Outcome.Ok("b.txt:2:the needle\nb.txt:3:needle needle\nsub/a.txt:1:needle in sub")


def test_gitignore_applies_even_outside_a_git_repository(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("secret.txt\nbuild/\n*.log\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.txt").write_text("needle")
    (tmp_path / "secret.txt").write_text("needle")
    (tmp_path / "run.log").write_text("needle")
    (tmp_path / "kept.txt").write_text("needle")
    assert call(GrepTool(tmp_path), pattern="needle") == Outcome.Ok("kept.txt:1:needle")


def test_a_nested_gitignore_and_a_negation(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / ".gitignore").write_text("*.gen\n!keep.gen\n")
    (tmp_path / "pkg" / "a.gen").write_text("needle")
    (tmp_path / "pkg" / "keep.gen").write_text("needle")
    assert call(GrepTool(tmp_path), pattern="needle") == Outcome.Ok("pkg/keep.gen:1:needle")


def test_hidden_files_and_binary_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / ".hidden").write_text("needle")
    (tmp_path / ".dir").mkdir()
    (tmp_path / ".dir" / "x.txt").write_text("needle")
    (tmp_path / "blob.bin").write_bytes(b"needle\x00\x01\x02")
    (tmp_path / "ok.txt").write_text("needle")
    out = call(GrepTool(tmp_path), pattern="needle")
    assert out == Outcome.Ok("ok.txt:1:needle\n[skipped 1 binary file(s)]")


def test_files_are_searched_as_named_even_when_ignored(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("secret.txt\n")
    (tmp_path / "secret.txt").write_text("a needle\n")
    out = call(GrepTool(tmp_path), pattern="needle", files=["secret.txt"])
    assert out == Outcome.Ok("secret.txt:1:a needle")


def test_ignore_case_glob_and_a_single_file_path(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text("Needle\n")
    (tmp_path / "a.txt").write_text("Needle\n")
    tool = GrepTool(tmp_path)
    assert ok(call(tool, pattern="needle")) == "No matches found in 2 file(s)"
    assert call(tool, pattern="needle", ignore_case=True, glob="*.rs") == Outcome.Ok(
        "a.rs:1:Needle"
    )
    assert call(tool, pattern="Needle", path="a.txt") == Outcome.Ok("a.txt:1:Needle")


def test_many_files_in_parallel_give_a_deterministic_answer(tmp_path: Path) -> None:
    for i in range(200):
        (tmp_path / f"f{i:03}.txt").write_text(f"line\nneedle {i}\n")
    out = ok(call(GrepTool(tmp_path), pattern="needle"))
    assert out.splitlines() == [f"f{i:03}.txt:2:needle {i}" for i in range(200)]


def test_errors_are_outcomes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    tool = GrepTool(tmp_path)
    cases: list[tuple[dict[str, object], str]] = [
        ({"pattern": "("}, "invalid regex"),
        ({"pattern": ""}, "must not be empty"),
        ({"pattern": "x", "path": "nope"}, "path not found: nope"),
        ({"pattern": "x", "files": ["nope"]}, "not a file: nope"),
        ({"pattern": "x", "files": []}, "`files` must not be empty"),
        ({"pattern": "x", "glob": "[z-a"}, "invalid glob"),
        ({}, "missing field `pattern`"),
    ]
    for args, message in cases:
        assert message in err(call(tool, **args)), args
