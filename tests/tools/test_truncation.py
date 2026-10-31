"""Output limits: grep keeps the head, bash the tail, the whole goes to a file; read just stops."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from calls import call, err, ok, parse_truncated

from tau_next import BashTool, GrepTool, ReadTool, WriteTool

LIMIT = 200


def numbered(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(1, n + 1))


def test_read_stops_after_the_last_whole_line_that_fits_and_says_where_to_go_on(
    tmp_path: Path,
) -> None:
    text = numbered(1000)
    (tmp_path / "big.txt").write_text(text)
    first = ok(call(ReadTool(tmp_path, max_output=LIMIT), path="big.txt"))
    kept, _, note = first.rpartition("\n[")
    assert text.startswith(kept)
    assert len(kept.encode()) <= LIMIT
    shown = kept.count("\n") + 1
    assert note == f"showing lines 1-{shown}; offset={shown + 1} continues]"
    # following the note reads on, with no gap and no overlap
    second = ok(call(ReadTool(tmp_path, max_output=LIMIT), path="big.txt", offset=shown + 1))
    assert second.startswith(f"line {shown + 1}\n")


def test_read_writes_no_overflow_file(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text(numbered(1000))
    done = subprocess.run(
        [sys.executable, "-c", READ_ONCE, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path / "run")},
    )
    assert "continues]" in done.stdout
    assert not (tmp_path / "run").exists()


READ_ONCE = """
import asyncio, json, sys
from tau_next import ReadTool
call = ReadTool(sys.argv[1], max_output=100).call(json.dumps({"path": "big.txt"}))
print(asyncio.run(call).value)
"""


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
    read = ok(call(ReadTool(tmp_path, max_output=10), path="f.txt"))
    assert read == "aaaa\nbbbb\n[showing lines 1-2; offset=3 continues]"
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
    out = call(ReadTool(tmp_path, max_output=200), path="f.txt")
    assert ok(out) == "x" * 200
    assert not overflow.exists()
    bash = BashTool(tmp_path, max_output=200, overflow_dir=overflow)
    assert ok(call(bash, command="printf 'y%.0s' $(seq 200)")) == "y" * 200
    assert not overflow.exists()
    assert "truncated" in ok(call(bash, command="printf 'y%.0s' $(seq 201)"))


@pytest.mark.parametrize("limit", [7, 9, 13, 50])
def test_a_cut_never_splits_a_utf8_character(tmp_path: Path, overflow: Path, limit: int) -> None:
    text = "日本語é\n" * 40
    (tmp_path / "u.txt").write_text(text)
    bash = BashTool(tmp_path, max_output=limit, overflow_dir=overflow)
    answer = parse_truncated(ok(call(bash, command="cat u.txt")))
    assert "�" not in answer.kept
    if answer.cut:  # no whole line fits: part of one is kept, from a character boundary
        assert limit < len("日本語é\n".encode())
        assert answer.kept.strip("\n") in text
    else:
        kept_lines = answer.kept.strip("\n").split("\n")
        assert kept_lines == ["日本語é"] * len(kept_lines)
    assert len(answer.kept.encode()) <= limit
    assert answer.full == text.encode()
    read = ok(call(ReadTool(tmp_path, max_output=limit), path="u.txt"))
    assert "�" not in read
    assert len(read.partition("\n[")[0].encode()) <= limit


def test_one_line_longer_than_the_limit_is_cut_on_a_character(
    tmp_path: Path, overflow: Path
) -> None:
    (tmp_path / "long.txt").write_text("é" * 100 + "\n")
    head = ok(call(ReadTool(tmp_path, max_output=15), path="long.txt"))
    assert head == "é" * 7 + (
        "\n[line 1 is cut at byte 14; read cannot show the rest (use bash: cut -c or tail -c);"
        " offset=2 continues after it]"
    )
    tail = parse_truncated(
        ok(call(BashTool(tmp_path, max_output=15, overflow_dir=overflow), command="cat long.txt"))
    )
    assert tail.cut
    assert tail.kept.rstrip("\n") == "é" * 7


SPILL_ONCE = """
import asyncio, json, sys
from tau_next import GrepTool
text = asyncio.run(GrepTool(sys.argv[1]).call(json.dumps({"pattern": "0123"}))).value
print(text.rsplit(" is in ", 1)[1].rstrip("]"))
"""


def spill_dir(tmp_path: Path, **env: str) -> Path:
    """The directory the overflow file of a default grep lands in, in a fresh process whose
    environment is the test's without its XDG_RUNTIME_DIR and TMPDIR, plus `env`."""
    tmp_path.mkdir()
    (tmp_path / "big.txt").write_text("0123456789\n" * 3000)
    base = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "TMPDIR")}
    done = subprocess.run(
        [sys.executable, "-c", SPILL_ONCE, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        env={**base, **env},
    )
    path = Path(done.stdout.strip())
    path.unlink()
    return path.parent


def test_the_default_directory_is_under_xdg_runtime_dir_when_that_is_set(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    assert spill_dir(tmp_path / "w1", XDG_RUNTIME_DIR=str(run)) == run / "tau"


def test_the_default_directory_is_tau_euid_in_the_temp_dir_otherwise(tmp_path: Path) -> None:
    temp = tmp_path / "tmp"
    temp.mkdir()
    assert spill_dir(tmp_path / "w2", TMPDIR=str(temp)) == temp / f"tau-{os.geteuid()}"


def test_old_overflow_files_are_removed_when_the_directory_is_opened(
    tmp_path: Path, overflow: Path
) -> None:
    (tmp_path / "f.txt").write_text("x" * 100)
    overflow.mkdir(mode=0o700)
    old, fresh, other = overflow / "old00000.out", overflow / "new00000.out", overflow / "keep.txt"
    for f in (old, fresh, other):
        f.write_text("x")
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    os.utime(other, (two_days_ago, two_days_ago))
    bash = BashTool(tmp_path, max_output=10, overflow_dir=overflow)
    assert "full output" in ok(call(bash, command="seq 1 100"))
    assert not old.exists()
    assert fresh.exists()
    assert other.exists(), "only overflow files are removed"


def test_an_overflow_directory_that_cannot_be_used_costs_the_file_not_the_answer(
    tmp_path: Path,
) -> None:
    (tmp_path / "f.txt").write_text("x" * 100)
    (tmp_path / "blocker").write_text("a file where the directory should be")
    unwritable = tmp_path / "blocker" / "tau"
    grep = GrepTool(tmp_path, max_output=10, overflow_dir=unwritable)
    bash = BashTool(tmp_path, max_output=10, overflow_dir=unwritable)
    for text in (ok(call(grep, pattern="x")), ok(call(bash, command="seq 1 100"))):
        assert "was not saved: the overflow directory" in text
        assert "could not be created" in text


def test_an_overflow_directory_open_to_others_or_a_symlink_is_never_written_into(
    tmp_path: Path,
) -> None:
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    for directory, why in ((open_dir, "has mode 0755"), (link, "is a symlink")):
        bash = BashTool(tmp_path, max_output=10, overflow_dir=directory)
        text = ok(call(bash, command="seq 1 100"))
        assert f"was not saved: the overflow directory {directory} {why}" in text
    assert list(open_dir.iterdir()) == []
    assert list(real.iterdir()) == []


def test_the_write_tool_has_no_limit_to_exceed(tmp_path: Path) -> None:
    out = ok(call(WriteTool(tmp_path), path="f.txt", content="x" * 100_000))
    assert out == "Wrote 1 line (100000 bytes) to f.txt"


FILE_SIZE_LIMITED = """
import asyncio, json, resource, sys
from tau_next import BashTool, GrepTool

_, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(resource.RLIMIT_FSIZE, (50_000, hard))  # a disk that fills up
overflow = sys.argv[1] + "/overflow"
bash = BashTool(sys.argv[1], max_output=1000, overflow_dir=overflow)
command = json.dumps({"command": "seq 1 100000; echo LAST; exit 3"})
print(asyncio.run(bash.call(command)))
grep = GrepTool(sys.argv[1], max_output=1000, overflow_dir=overflow)
print(asyncio.run(grep.call(json.dumps({"pattern": "hit"}))))
# The disk empties again: an instrumented build (scripts/coverage.sh) writes its profile at exit.
resource.setrlimit(resource.RLIMIT_FSIZE, (hard, hard))
"""


def test_a_full_disk_costs_the_overflow_file_not_the_answer(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("hit\n" * 100000)
    done = subprocess.run(
        [sys.executable, "-c", FILE_SIZE_LIMITED, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    bash, grep = done.stdout.splitlines()
    for answer in (bash, grep):
        assert "was not saved after 50000 bytes: could not write the full output" in answer
    assert "LAST" in bash
    assert "command exited with code 3" in bash
    assert "big.txt:1:hit" in grep
