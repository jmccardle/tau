"""Run the examples in the guides (Phase 7, 7C-3): `docs/guide/*.md`.

    scripts/guide_examples.py              run every guide
    scripts/guide_examples.py FILE ...     run these guides
    scripts/guide_examples.py -v ...       also print what each block printed

A fenced block whose language is `python` or `bash` runs, in the order the guide gives them, in a
fresh temporary directory per guide: a working directory, and a `TAU_HOME` of its own that
starts empty (a guide that needs a config writes it, as a reader would). `HOME` is that
directory too, no API key is in the environment, `tau` is the one of the Python that runs this
script, and a block that does not exit 0 fails the gate. Blocks of one guide share the
directory, so a later block can use a file an earlier one wrote.

The words after the language are the block's options:

    ```python skip           shown, never run (a fragment, or a program that needs a real model)
    ```python continues      the previous python block, and this one, run as one program (a guide
                             that explains a program in pieces)
    ```json file=PATH        not run: written to PATH first. PATH is relative to the working
                             directory, or `$TAU_HOME/...`. Any language.
    ```text expect           what the block just above it, which must be a python or bash block
                             that ran, prints on stdout. Compared exactly, line by line, except
                             that trailing spaces and trailing blank lines do not count and the
                             token `<*>` stands for any run of characters within a line (an id, a
                             timestamp, a temporary path). Any language, `text` by convention.

Every other language (`text`, `toml`, ...) is shown and never run. A guide that starts a server
stops it itself; the runner also stops any that is left. The guides' words are held to rule 3's
list too: a line of a guide may use a banned word for another meaning by saying so in a comment on
that line, as the code does (`<!-- rules: allow(entry): a Python packaging term -->`).
"""

import contextlib
import difflib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from check_rules import banned_terms

ROOT = Path(__file__).parents[1]
GUIDES = ROOT / "docs" / "guide"
TIMEOUT = 90  # seconds for one block
FENCE = re.compile(r"^(?P<indent>\s*)```(?P<lang>[\w+-]*)\s*(?P<options>.*?)\s*$")
RUNS = ("python", "bash")
VERBOSE = False


class GuideError(Exception):
    """A guide that cannot be read: an unknown option, an unclosed fence."""


@dataclass(frozen=True)
class Block:
    path: Path
    line: int  # the line of the opening fence
    lang: str
    skip: bool
    file: str | None
    continues: bool
    expect: bool
    text: str

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}"

    @property
    def runs(self) -> bool:
        return self.lang in RUNS and not self.skip and self.file is None


@dataclass
class Report:
    ran: int = 0
    checked: int = 0
    written: int = 0
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def parse_options(where: str, text: str) -> tuple[bool, str | None, bool, bool]:
    """(skip, file, continues, expect) from the words after a fence's language."""
    skip, file, continues, expect = False, None, False, False
    for word in text.split():
        if word == "skip":
            skip = True
        elif word == "continues":
            continues = True
        elif word == "expect":
            expect = True
        elif word.startswith("file=") and len(word) > len("file="):
            file = word.removeprefix("file=")
        else:
            raise GuideError(
                f"{where}: unknown block option {word!r} (skip, continues, expect, file=PATH)"
            )
    if expect and (skip or file or continues):
        raise GuideError(f"{where}: `expect` stands alone: it checks the block above it")
    return skip, file, continues, expect


def blocks(path: Path) -> list[Block]:
    """Every fenced block of a guide, in order."""
    found: list[Block] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        opening = FENCE.match(lines[i])
        if opening is None:
            i += 1
            continue
        where = f"{path.relative_to(ROOT)}:{i + 1}"
        skip, file, continues, expect = parse_options(where, opening["options"])
        start, body = i + 1, []
        i += 1
        while i < len(lines) and not re.fullmatch(r"\s*```\s*", lines[i]):
            body.append(lines[i].removeprefix(opening["indent"]))
            i += 1
        if i == len(lines):
            raise GuideError(f"{where}: this block is never closed")
        found.append(
            Block(
                path, start, opening["lang"], skip, file, continues, expect, "\n".join(body) + "\n"
            )
        )
        i += 1
    return merged(found)


def merged(found: list[Block]) -> list[Block]:
    """A `continues` block joins the python block before it."""
    out: list[Block] = []
    for block in found:
        if not block.continues:
            out.append(block)
            continue
        if not out or out[-1].lang != "python" or block.lang != "python" or out[-1].file:
            raise GuideError(f"{block.where}: `continues` follows a python block")
        before = out.pop()
        out.append(replace(before, text=before.text + "\n" + block.text, skip=before.skip))
    return out


def environment(home: Path, tmp: Path) -> dict[str, str]:
    """Nothing from the caller's environment but the way to find programs: no key, no TAU_HOME."""
    bin_dir = str(Path(sys.executable).parent)
    return {
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp),
        "TAU_HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONUTF8": "1",
    }


def target(block: Block, home: Path, work: Path) -> Path:
    assert block.file is not None
    if block.file.startswith("$TAU_HOME/"):
        return home / block.file.removeprefix("$TAU_HOME/")
    return work / block.file


def command(block: Block) -> list[str]:
    if block.lang == "python":
        return [sys.executable, "-c", block.text]
    return ["bash", "-euo", "pipefail", "-c", block.text]


def run_block(block: Block, env: dict[str, str], work: Path) -> tuple[str | None, str]:
    """(the failure of one block as a message or None if it exited 0, what it printed)."""
    try:
        done = subprocess.run(
            command(block),
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"{block.where}: still running after {TIMEOUT} seconds", ""
    if done.returncode == 0:
        if VERBOSE and (done.stdout or done.stderr):
            print(f"--- {block.where}\n{done.stdout}{done.stderr}", end="")
        return None, done.stdout
    tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-25:])
    return f"{block.where}: {block.lang} block exited {done.returncode}\n{tail}", ""


def lines_of(text: str) -> list[str]:
    """Lines without trailing spaces, and without trailing blank lines."""
    return [line.rstrip() for line in text.rstrip().splitlines()]


def mismatch(block: Block, printed: str) -> str | None:
    """Why an `expect` block differs from what the block above it printed, or None."""
    want, got = lines_of(block.text), lines_of(printed)
    pattern = [re.compile(".*".join(re.escape(part) for part in w.split("<*>"))) for w in want]
    if len(want) == len(got) and all(p.fullmatch(g) for p, g in zip(pattern, got, strict=True)):
        return None
    diff = difflib.unified_diff(want, got, "expected (the guide)", "printed", lineterm="", n=1)
    return f"{block.where}: the block above printed something else\n" + "\n".join(diff)


def stop_servers(home: Path) -> None:
    """A guide stops what it starts; this stops what a failure left."""
    for record in (home / "serve").glob("*.json"):
        match = re.search(r'"pid":\s*(\d+)', record.read_text(encoding="utf-8"))
        if match:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(match[1]), signal.SIGTERM)
    time.sleep(0.2)


def run_guide(path: Path) -> Report:
    report = Report()
    with tempfile.TemporaryDirectory(prefix="tau-guide-") as name:
        tmp = Path(name)
        home, work = tmp / "home", tmp / "work"
        home.mkdir(mode=0o700)
        work.mkdir()
        env = environment(home, tmp)
        printed: str | None = None  # stdout of the block just above, if it ran
        try:
            for block in blocks(path):
                above, printed = printed, None
                if block.expect:
                    if above is None:
                        raise GuideError(f"{block.where}: `expect` must follow a block that ran")
                    report.checked += 1
                    failure = mismatch(block, above)
                    if failure:
                        report.failures.append(failure)
                elif block.skip and block.lang in RUNS:
                    report.skipped.append(block.where)
                elif block.file is not None:
                    out = target(block, home, work)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(block.text, encoding="utf-8")
                    report.written += 1
                elif block.runs:
                    report.ran += 1
                    failure, printed = run_block(block, env, work)
                    if failure:
                        report.failures.append(failure)
                        break  # later blocks build on this one
        finally:
            stop_servers(home)
    return report


def prose_problems(path: Path) -> list[str]:
    """Rule 3's banned words, in every line of a guide."""
    return [
        f"{path.relative_to(ROOT)}:{n}: banned term {term!r}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for term in banned_terms(line)
    ]


def main(args: list[str]) -> int:
    global VERBOSE  # noqa: PLW0603 - a command-line switch, read by run_block
    VERBOSE = "-v" in args
    args = [a for a in args if a != "-v"]
    paths = [Path(a).resolve() for a in args] or sorted(GUIDES.glob("*.md"))
    problems = [p for path in paths for p in prose_problems(path)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            reports = dict(zip(paths, pool.map(run_guide, paths), strict=True))
    except GuideError as e:
        print(f"guide_examples: {e}", file=sys.stderr)
        return 1
    problems += [f for r in reports.values() for f in r.failures]
    for problem in problems:
        print(problem, file=sys.stderr)
    skipped = [s for r in reports.values() for s in r.skipped]
    for where in skipped:
        print(f"guide_examples: skipped {where}")
    ran = sum(r.ran for r in reports.values())
    written = sum(r.written for r in reports.values())
    checked = sum(r.checked for r in reports.values())
    print(
        f"guide_examples: {ran} blocks ran, {checked} outputs checked, {written} files written, "
        f"{len(skipped)} skipped in {len(paths)} guides"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
