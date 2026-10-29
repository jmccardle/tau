"""Failure modes of the base tools: escaped processes, signals, private overflow files, caps,
memory, special files, cancellation."""

import asyncio
import json
import os
import resource
import stat
import subprocess
import time
from pathlib import Path

import pytest
from calls import call, err, ok, parse_truncated

from tau_next import BashTool, GrepTool, Outcome, ReadTool, WriteTool

ESCAPED = "a process outside the command's group still holds the output open"


def alive(pid: int) -> bool:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat_line.rsplit(")", 1)[1].split()[0] != "Z"


def gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def end(pid: int) -> None:
    subprocess.run(["kill", str(pid)], check=False)


# bash: a process that left the group


def test_a_process_outside_the_group_does_not_hold_the_timeout_hostage(tmp_path: Path) -> None:
    started = time.monotonic()
    out = call(
        BashTool(tmp_path),
        command="setsid sleep 13 & echo $! > pid; sleep 30",
        timeout=500,
    )
    assert time.monotonic() - started < 5
    assert "command timed out after 500 ms" in err(out)
    assert ESCAPED in err(out)
    end(int((tmp_path / "pid").read_text()))


def test_cancelling_while_a_process_outside_the_group_holds_the_pipe(tmp_path: Path) -> None:
    async def scenario() -> float:
        tool = BashTool(tmp_path)
        command = "setsid sleep 13 & echo $! > pid; sleep 30"
        running = asyncio.create_task(tool.call(json.dumps({"command": command})))
        while not (tmp_path / "pid").exists():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        started = time.monotonic()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5
    end(int((tmp_path / "pid").read_text()))


def test_a_normal_exit_with_an_escaped_process_is_an_ok_with_a_note(tmp_path: Path) -> None:
    out = ok(
        call(BashTool(tmp_path), command="setsid sleep 13 & echo $! > pid; sleep 0.3; echo done")
    )
    assert out == f"done\n\n[output may be cut off: {ESCAPED}]"
    end(int((tmp_path / "pid").read_text()))


# bash: signals, terminal, timeout limits


def test_sigterm_comes_first_then_sigkill_after_the_grace(tmp_path: Path) -> None:
    polite = BashTool(tmp_path, kill_grace_ms=3000)
    command = "trap 'echo got term; exit 0' TERM; echo ready; while :; do sleep 0.05; done"
    out = err(call(polite, command=command, timeout=300))
    assert out.startswith("ready\n"), out
    assert "got term\n" in out, out

    stubborn = BashTool(tmp_path, kill_grace_ms=400)
    command = "trap '' TERM; while :; do sleep 0.05; done"
    started = time.monotonic()
    err(call(stubborn, command=command, timeout=200))
    elapsed = time.monotonic() - started
    assert 0.5 < elapsed < 5, elapsed


def test_the_shell_is_a_session_leader_without_a_terminal(tmp_path: Path) -> None:
    out = ok(call(BashTool(tmp_path), command="cat /proc/$$/stat"))
    pid = out.split()[0]
    pgrp, session, tty = out.rsplit(") ", 1)[1].split()[2:5]
    assert (pgrp, session, tty) == (pid, pid, "0")


def test_a_timeout_over_the_maximum_is_an_error_naming_it(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, timeout_ms=500, max_timeout_ms=1000)
    assert err(call(tool, command="true", timeout=1001)) == (
        "`timeout` of 1001 ms is over the maximum of 1000 ms"
    )
    assert ok(call(tool, command="true", timeout=1000)) == ""
    assert "at most 1000" in tool.description
    with pytest.raises(ValueError, match="over the maximum"):
        BashTool(tmp_path, timeout_ms=2000, max_timeout_ms=1000)


def test_each_call_has_its_own_thread(tmp_path: Path) -> None:
    async def scenario() -> float:
        tool = BashTool(tmp_path)
        started = time.monotonic()
        calls = [tool.call(json.dumps({"command": "sleep 0.4"})) for _ in range(100)]
        outcomes = await asyncio.gather(*calls)
        assert outcomes == [Outcome.Ok("")] * 100
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1.5


# overflow file: private, capped


def test_the_overflow_directory_and_file_are_private(tmp_path: Path) -> None:
    overflow = tmp_path / "ov"
    (tmp_path / "f.txt").write_text("x\n" * 100)
    answer = parse_truncated(
        ok(call(ReadTool(tmp_path, max_output=10, overflow_dir=overflow), path="f.txt"))
    )
    assert stat.S_IMODE(overflow.stat().st_mode) == 0o700
    assert stat.S_IMODE(answer.path.stat().st_mode) == 0o600


def test_a_directory_open_to_others_is_an_error_with_no_fallback(tmp_path: Path) -> None:
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    (tmp_path / "f.txt").write_text("x\n" * 100)
    tool = ReadTool(tmp_path, max_output=10, overflow_dir=open_dir)
    assert "has mode 0755" in err(call(tool, path="f.txt"))
    assert list(open_dir.iterdir()) == []


def test_a_symlink_as_the_overflow_directory_is_an_error(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    (tmp_path / "f.txt").write_text("x\n" * 100)
    tool = BashTool(tmp_path, max_output=10, overflow_dir=link)
    assert "is a symlink" in err(call(tool, command="seq 1 100"))
    assert list(real.iterdir()) == []


def test_the_overflow_file_is_capped_and_the_note_says_so(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, max_output=100, overflow_dir=tmp_path / "ov", max_overflow=1000)
    answer = parse_truncated(ok(call(tool, command="seq 1 5000")))
    full = "".join(f"{i}\n" for i in range(1, 5001))
    assert answer.path.read_bytes() == full.encode()[:1000]
    assert answer.size == len(full)
    assert answer.omitted == 5000 - answer.kept.count("\n")
    text = ok(call(tool, command="seq 1 5000"))
    assert f"the output is {len(full)} bytes but " in text
    assert "holds only the first 1000 bytes]" in text
    with pytest.raises(ValueError, match="overflow cap"):
        ReadTool(tmp_path, max_output=100, max_overflow=99)


def test_bash_with_endless_output_cannot_fill_the_disk(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, max_output=100, overflow_dir=tmp_path / "ov", max_overflow=100_000)
    out = err(call(tool, command="yes", timeout=1000))
    answer = parse_truncated(out)
    assert answer.path.stat().st_size == 100_000
    assert "holds only the first 100000 bytes" in out


# read


def test_the_hint_continues_after_the_kept_lines_when_the_byte_limit_cut(tmp_path: Path) -> None:
    (tmp_path / "n.txt").write_text("".join(f"line {i}\n" for i in range(1, 200)))
    tool = ReadTool(tmp_path, max_output=40, overflow_dir=tmp_path / "ov")
    out = ok(call(tool, path="n.txt"))
    kept = parse_truncated(out.rsplit("\n[showing", 1)[0]).kept
    shown = kept.count("\n") + 1
    assert out.endswith(f"\n[showing lines 1-{shown}; offset={shown + 1} continues]")
    follow = ok(call(tool, path="n.txt", offset=shown + 1))
    assert follow.startswith(f"line {shown + 1}\n")
    assert "were read" not in out


def test_a_file_of_one_enormous_line_is_not_held_in_memory(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_bytes(b"x" * 200_000_000)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out = ok(
        call(
            ReadTool(tmp_path, overflow_dir=tmp_path / "ov", max_overflow=1_000_000), path="one.txt"
        )
    )
    grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
    assert grown_mb < 50, grown_mb
    assert "holds only the first 1000000 bytes" in out


def test_a_pipe_is_refused_not_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    assert "not a regular file" in err(call(ReadTool(tmp_path), path="pipe"))


def test_invalid_utf8_is_replaced_and_said_so(tmp_path: Path) -> None:
    (tmp_path / "l.txt").write_bytes(b"caf\xe9\n")
    out = ok(call(ReadTool(tmp_path), path="l.txt"))
    assert out == "caf�\n\n[invalid UTF-8 in the output was replaced with U+FFFD]"
    out = ok(call(BashTool(tmp_path), command="printf 'caf\\351\\n'"))
    assert out.startswith("[invalid UTF-8 in the output was replaced with U+FFFD]\ncaf�")


# cancelling read and grep


def test_cancelling_a_grep_over_a_big_tree_returns_promptly(tmp_path: Path) -> None:
    for d in range(50):
        sub = tmp_path / f"d{d}"
        sub.mkdir()
        for f in range(200):
            (sub / f"f{f}.txt").write_text("nothing here\n" * 10)

    async def scenario() -> float:
        tool = GrepTool(tmp_path)
        running = asyncio.create_task(tool.call(json.dumps({"pattern": "zzz"})))
        await asyncio.sleep(0)
        started = time.monotonic()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 2


# grep


def test_paths_are_relative_to_the_working_directory_not_the_searched_path(tmp_path: Path) -> None:
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "deep" / "lib.rs").write_text("needle\n")
    tool = GrepTool(tmp_path)
    expected = Outcome.Ok("src/deep/lib.rs:1:needle")
    assert call(tool, pattern="needle", path="src") == expected
    assert call(tool, pattern="needle", path="src/deep") == expected
    assert call(tool, pattern="needle", path="src/deep/lib.rs") == expected
    assert call(tool, pattern="needle", files=["./src/deep/lib.rs"]) == expected


def test_a_glob_filters_but_never_overrides_gitignore_or_hidden(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("gen.rs\n")
    for name in ("gen.rs", ".hid.rs", "main.rs", "main.txt"):
        (tmp_path / name).write_text("needle\n")
    out = call(GrepTool(tmp_path), pattern="needle", glob="*.rs")
    assert out == Outcome.Ok("main.rs:1:needle")


def test_a_minified_line_is_cut_with_a_marker(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("needle " + "a" * 5000 + "\n")
    line = ok(call(GrepTool(tmp_path), pattern="needle"))
    assert line.startswith("min.js:1:needle aaa")
    assert line.endswith(" [+4507 bytes]")


def test_binary_files_are_counted_and_named_when_named(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"\0needle")
    (tmp_path / "b.txt").write_text("needle\n")
    assert ok(call(GrepTool(tmp_path), pattern="needle")) == (
        "b.txt:1:needle\n[skipped 1 binary file(s)]"
    )
    out = ok(call(GrepTool(tmp_path), pattern="needle", files=["a.bin"]))
    assert out.endswith("[skipped binary files: a.bin]")


def test_matching_that_stopped_at_a_nul_is_noted(tmp_path: Path) -> None:
    (tmp_path / "m.dat").write_bytes(b"needle\n" * 50_000 + b"\0needle\n")
    tool = GrepTool(tmp_path, max_output=100_000_000, max_overflow=200_000_000)
    out = ok(call(tool, pattern="needle", path="m.dat"))
    assert "stopped at a NUL byte at offset" in out.splitlines()[-1]


def test_the_search_stops_at_the_overflow_cap(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("hit\n" * 100_000)
    tool = GrepTool(tmp_path, max_output=100, overflow_dir=tmp_path / "ov", max_overflow=5000)
    out = ok(call(tool, pattern="hit"))
    assert "[search stopped: the matches reached 5000 bytes" in out


# write


def test_write_goes_through_a_dangling_symlink(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to("target.txt")
    out = ok(call(WriteTool(tmp_path), path="link", content="made\n"))
    assert out == "Wrote 1 line (5 bytes) to link"
    assert (tmp_path / "link").is_symlink()
    assert (tmp_path / "target.txt").read_text() == "made\n"


def test_write_keeps_a_read_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "ro.txt"
    path.write_text("old")
    path.chmod(0o444)
    ok(call(WriteTool(tmp_path), path="ro.txt", content="new"))
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
