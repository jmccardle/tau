"""tau-core is sans-IO: it depends on no IO, async, or binding crate.

Two checks, both from CLAUDE.md's layout table ("No IO, no async runtime, no PyO3"):

1. `cargo metadata`: the normal + build dependency closure of tau-core contains
   none of DENIED (dev-dependencies are not shipped and are exempt, but are
   listed). Prints the closure.
2. A text scan of crates/tau-core/src for std::fs, std::net, std::process,
   std::io, std::thread, std::env and `async`/`.await`. Test modules in src
   are scanned too. A line may opt out only with a reason on the same line:
   `// core-io: allow(<reason>)`, for code that never ships (a test-only
   feature such as `transitions`).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
CRATE = "tau-core"
DENIED = {
    "tokio",
    "async-std",
    "smol",
    "futures",
    "futures-executor",
    "futures-util",
    "mio",
    "pyo3",
    "pyo3-ffi",
    "libc",
    "rustix",
    "nix",
    "socket2",
    "reqwest",
    "hyper",
    "memmap2",
    "walkdir",
    "ignore",
    "tempfile",
    "fs2",
    "fs4",
    "rayon",
    "crossbeam",
    "crossbeam-channel",
    "tau-store",
    "tau-tools",
    "tau-py",
}
SOURCE_PATTERNS = {
    "std::fs": r"\bstd::fs\b",
    "std::net": r"\bstd::net\b",
    "std::process": r"\bstd::process\b",
    "std::io": r"\bstd::io\b",
    "std::thread": r"\bstd::thread\b",
    "std::env": r"\bstd::env\b",
    "async": r"\basync\s+(?:fn|move|\{)|\.await\b",
}


def closure(kinds: set[str]) -> set[str]:
    """Package names reachable from CRATE over dependency edges of the given kinds."""
    meta = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--format-version", "1", "--locked"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    names = {p["id"]: p["name"] for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root = next(i for i, n in names.items() if n == CRATE)
    seen, todo = {root}, [root]
    while todo:
        for dep in nodes[todo.pop()]["deps"]:
            if any(k["kind"] in kinds for k in dep["dep_kinds"]) and dep["pkg"] not in seen:
                seen.add(dep["pkg"])
                todo.append(dep["pkg"])
    return {names[i] for i in seen} - {CRATE}


def main() -> int:
    shipped = closure({None, "build"})
    with_dev = closure({None, "build", "dev"})
    bad = sorted(shipped & DENIED)
    problems = [f"{CRATE} depends on IO/async/binding crate {b}" for b in bad]
    for f in sorted((ROOT / "crates" / CRATE / "src").rglob("*.rs")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("//")[0]
            if re.search(r"core-io: allow\(.+\)", line):
                continue
            for label, pat in SOURCE_PATTERNS.items():
                if re.search(pat, code):
                    problems.append(f"{f.relative_to(ROOT)}:{n}: uses {label}")
    if "--list" in sys.argv:
        print("shipped:", ", ".join(sorted(shipped)))
        print("dev only:", ", ".join(sorted(with_dev - shipped)))
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(
            f"{CRATE} is sans-IO: {len(shipped)} shipped dependencies, none denied; "
            "no std IO in src"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
