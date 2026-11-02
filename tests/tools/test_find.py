"""`find`: paths matching a glob, walked the way `grep` walks."""

from pathlib import Path

import pytest
from calls import call, err, ok, parse_truncated

from tau import FindTool, Outcome


def touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


def test_a_glob_without_a_slash_matches_file_names_at_any_depth_sorted(tmp_path: Path) -> None:
    touch(tmp_path, "b.rs", "a.rs", "a.txt", "src/deep/c.rs", "src/d.rs")
    out = call(FindTool(tmp_path), pattern="*.rs")
    assert out == Outcome.Ok("a.rs\nb.rs\nsrc/d.rs\nsrc/deep/c.rs")


def test_a_glob_with_a_slash_matches_the_path_below_the_searched_directory(tmp_path: Path) -> None:
    touch(tmp_path, "main.rs", "src/lib.rs", "src/deep/c.rs", "other/x.rs")
    tool = FindTool(tmp_path)
    assert call(tool, pattern="src/**/*.rs") == Outcome.Ok("src/deep/c.rs\nsrc/lib.rs")
    assert call(tool, pattern="src/*.rs") == Outcome.Ok("src/lib.rs")
    assert call(tool, pattern="**/*.rs") == Outcome.Ok(
        "main.rs\nother/x.rs\nsrc/deep/c.rs\nsrc/lib.rs"
    )


def test_paths_are_relative_to_the_searched_directory(tmp_path: Path) -> None:
    touch(tmp_path, "src/lib.rs", "src/deep/c.rs", "top.rs")
    tool = FindTool(tmp_path)
    assert call(tool, pattern="*.rs", path="src") == Outcome.Ok("deep/c.rs\nlib.rs")
    assert call(tool, pattern="deep/*.rs", path="./src") == Outcome.Ok("deep/c.rs")
    absolute = str(tmp_path / "src" / "deep")
    assert call(tool, pattern="*.rs", path=absolute) == Outcome.Ok("c.rs")


def test_the_walk_honors_ignore_rules_without_a_git_repository(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("gen.rs\nbuild/\n")
    (tmp_path / ".ignore").write_text("scratch.rs\n")
    touch(tmp_path, "gen.rs", "build/out.rs", "scratch.rs", "kept.rs")
    assert call(FindTool(tmp_path), pattern="*.rs") == Outcome.Ok("kept.rs")


def test_hidden_files_and_the_contents_of_hidden_directories_are_listed(tmp_path: Path) -> None:
    touch(tmp_path, ".env", ".github/workflows/ci.yml", ".hidden.rs", ".dir/x.rs", "kept.rs")
    tool = FindTool(tmp_path)
    assert call(tool, pattern=".env") == Outcome.Ok(".env")
    assert call(tool, pattern="*.yml") == Outcome.Ok(".github/workflows/ci.yml")
    assert call(tool, pattern=".github/**") == Outcome.Ok(".github/workflows/ci.yml")
    assert call(tool, pattern="*.rs") == Outcome.Ok(".dir/x.rs\n.hidden.rs\nkept.rs")


def test_hidden_files_are_still_subject_to_ignore_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".env\n.cache/\n")
    touch(tmp_path, ".env", ".cache/x.txt", ".env.example")
    assert call(FindTool(tmp_path), pattern=".env*") == Outcome.Ok(".env.example")


def test_the_git_directory_is_never_listed(tmp_path: Path) -> None:
    touch(tmp_path, ".git/config", ".git/objects/ab/cd", "kept.txt")
    tool = FindTool(tmp_path)
    assert call(tool, pattern="*") == Outcome.Ok("kept.txt")
    assert call(tool, pattern="config") == Outcome.Ok('No files found matching "config"')


def test_a_nested_gitignore_and_a_negation(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / ".gitignore").write_text("*.gen\n!keep.gen\n")
    touch(tmp_path, "pkg/a.gen", "pkg/keep.gen")
    assert call(FindTool(tmp_path), pattern="*.gen") == Outcome.Ok("pkg/keep.gen")


def test_directories_are_not_listed_and_symlinks_are_listed_not_followed(tmp_path: Path) -> None:
    touch(tmp_path, "real/inner.txt", "file.txt")
    (tmp_path / "link.txt").symlink_to("file.txt")
    (tmp_path / "linked").symlink_to("real")
    tool = FindTool(tmp_path)
    assert call(tool, pattern="*.txt") == Outcome.Ok("file.txt\nlink.txt\nreal/inner.txt")
    assert call(tool, pattern="real") == Outcome.Ok('No files found matching "real"')
    assert call(tool, pattern="linked") == Outcome.Ok("linked")
    assert call(tool, pattern="linked/*") == Outcome.Ok('No files found matching "linked/*"')


def test_a_named_directory_is_searched_even_inside_an_ignored_one(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("build/\n")
    touch(tmp_path, "build/out.rs")
    assert call(FindTool(tmp_path), pattern="*.rs") == Outcome.Ok('No files found matching "*.rs"')
    assert call(FindTool(tmp_path), pattern="*.rs", path="build") == Outcome.Ok("out.rs")


def test_no_match_is_a_plain_answer_not_an_error(tmp_path: Path) -> None:
    touch(tmp_path, "a.txt")
    assert call(FindTool(tmp_path), pattern="*.zzz") == Outcome.Ok(
        'No files found matching "*.zzz"'
    )


def test_errors_name_the_problem(tmp_path: Path) -> None:
    touch(tmp_path, "f")
    tool = FindTool(tmp_path)
    cases: list[tuple[dict[str, object], str]] = [
        ({"pattern": "[z-a"}, "invalid glob"),
        ({"pattern": ""}, "`pattern` must not be empty"),
        ({"pattern": "/etc/*.conf"}, "relative to `path`"),
        ({"pattern": str(tmp_path / "f")}, "pass the directory as `path`"),
        ({"pattern": "*", "path": "nope"}, "path not found: nope"),
        ({"pattern": "*", "path": "f"}, "f is a file, not a directory"),
        ({}, "missing field `pattern`"),
        ({"pattern": "*", "type": "f"}, "unknown field"),
    ]
    for args, message in cases:
        assert message in err(call(tool, **args)), args


def test_a_long_list_keeps_the_head_and_the_file_holds_every_path(
    tmp_path: Path, overflow: Path
) -> None:
    touch(tmp_path, *(f"f{i:03}.txt" for i in range(200)))
    tool = FindTool(tmp_path, max_output=100, overflow_dir=overflow)
    answer = parse_truncated(ok(call(tool, pattern="*.txt")))
    assert answer.where == "after"
    assert answer.kept.startswith("f000.txt\nf001.txt\n")
    assert len(answer.kept.encode()) <= 100
    assert answer.full.decode().splitlines() == [f"f{i:03}.txt" for i in range(200)]


def test_the_search_stops_when_the_paths_reach_the_overflow_cap(
    tmp_path: Path, overflow: Path
) -> None:
    touch(tmp_path, *(f"file{i:04}.txt" for i in range(500)))
    tool = FindTool(tmp_path, max_output=100, overflow_dir=overflow, max_overflow=1000)
    out = ok(call(tool, pattern="*.txt"))
    assert "[search stopped: the paths reached 1000 bytes" in out


def test_a_missing_working_directory_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        FindTool(tmp_path / "nope")
