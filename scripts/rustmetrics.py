"""Rust source metrics for crates/, shared by ratchet.py and metrics.sh.

Everything is measured with `rust-code-analysis-cli` (JSON per file) plus the
source text for what the tool does not say (test modules, trait impls).

Definitions, so the numbers can be reproduced and argued with:

* Function CC: 1 + decision points anywhere in the function's span, closures
  included (radon does the same for nested Python functions). rust-code-analysis
  gives every closure a baseline 1 of its own; those are subtracted.
* Function cognitive: rust-code-analysis `cognitive.sum` for the function span.
* Function LOC: `loc.sloc` (physical lines in the span).
* Only top-level functions and methods are reported; a closure or nested fn is
  part of its enclosing function.
* "tests" are two things: files under `crates/*/tests` and `crates/*/benches`,
  and `#[cfg(test)] mod` blocks inside `crates/*/src` (which must run to the
  end of the file; anything else is an error). Source metrics exclude both
  (file line counts are the exception: a file is its whole length).
* Methods per type: functions directly inside `impl` blocks, grouped by
  (crate, type) across blocks. `impl Trait for Type` blocks are counted
  separately from inherent `impl Type` blocks, so a type has two counts.
  Generics, lifetimes and module paths are dropped from the type name.
* Limits of the tool: it parses macro bodies as token trees, so functions
  inside `proptest! { ... }` are not measured.
"""

import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]
CRATES = ROOT / "crates"
# bench-formats is excluded from the workspace (its own lockfile): not measured.
SKIP_CRATES = {"bench-formats"}
TEST_DIRS = ("tests", "benches")
CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]\s*$")
MOD_OPEN = re.compile(r"^\s*(?:pub\s+)?mod\s+\w+\s*\{")


class MetricsError(Exception):
    """The source or the tool did something this script does not understand."""


def crate_dirs() -> list[Path]:
    return sorted(
        p for p in CRATES.iterdir() if (p / "Cargo.toml").exists() and p.name not in SKIP_CRATES
    )


def rs_files(crate: Path, sub: str) -> list[Path]:
    d = crate / sub
    return sorted(d.rglob("*.rs")) if d.is_dir() else []


def rca_version() -> str:
    exe = shutil.which("rust-code-analysis-cli")
    if exe is None:
        raise MetricsError(
            "rust-code-analysis-cli not on PATH (cargo install --locked rust-code-analysis-cli)"
        )
    return subprocess.run(
        [exe, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()


def run_rca(paths: list[Path]) -> dict[Path, dict]:
    """rust-code-analysis JSON for each file."""
    rca_version()
    out: dict[Path, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for f in paths:
            # One call per file keeps the output name unambiguous.
            subprocess.run(
                ["rust-code-analysis-cli", "-m", "-p", str(f), "-O", "json", "-o", tmp],
                check=True,
                capture_output=True,
            )
            made = list(Path(tmp).rglob("*.json"))
            if len(made) != 1:
                raise MetricsError(f"expected one JSON for {f}, got {made}")
            out[f] = json.loads(made[0].read_text())
            made[0].unlink()
    return out


def test_module_start(path: Path) -> int | None:
    """Line (1-based) of a `#[cfg(test)] mod x {` that runs to end of file, else None."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if CFG_TEST.match(line) and i + 1 < len(lines) and MOD_OPEN.match(lines[i + 1]):
            depth = 0
            for j in range(i + 1, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if (
                    depth == 0
                    and j < len(lines) - 1
                    and any(rest.strip() for rest in lines[j + 1 :])
                ):
                    raise MetricsError(
                        f"{path}:{i + 1}: #[cfg(test)] module does not run to end of file"
                    )
            return i + 1
    return None


def _count_spaces(space: dict) -> int:
    return 1 + sum(_count_spaces(c) for c in space["spaces"])


def _function(space: dict, rel: str, owner: str | None) -> dict:
    nested = sum(_count_spaces(c) for c in space["spaces"] if c["kind"] == "function")
    m = space["metrics"]
    return {
        "file": rel,
        "name": space["name"],
        "owner": owner,
        "line": space["start_line"],
        "end": space["end_line"],
        "cc": int(m["cyclomatic"]["sum"]) - nested,
        "cognitive": int(m["cognitive"]["sum"]),
        "loc": int(m["loc"]["sloc"]),
    }


def _impl_kind(path_lines: list[str], space: dict) -> str:
    """'trait' for `impl .. for ..`, else 'inherent'. Reads the header up to `{`."""
    header = ""
    for line in path_lines[space["start_line"] - 1 :]:
        header += " " + line
        if "{" in line:
            break
    return "trait" if re.search(r"\bfor\b", header.split("{")[0]) else "inherent"


def _type_name(raw: str) -> str:
    name = re.sub(r"<[^<>]*>", "", raw)  # generics and lifetimes (names like Search<'a>)
    return name.split("::")[-1].strip()


def _functions_of(tree: dict, lines: list[str]) -> list[tuple[dict, str | None, str | None]]:
    """(function record, impl type, impl kind) for each top-level function or method in a file."""
    out: list[tuple[dict, str | None, str | None]] = []
    for c in tree["spaces"]:
        if c["kind"] == "impl":
            kind, ty = _impl_kind(lines, c), _type_name(c["name"])
            out += [(m, ty, kind) for m in c["spaces"] if m["kind"] == "function"]
        elif c["kind"] == "function":
            out.append((c, None, None))
    return out


def _measure_file(path: Path, tree: dict, crate: str, kind: str, result: dict) -> None:
    rel = str(path.relative_to(ROOT))
    lines = path.read_text().splitlines()
    cut = test_module_start(path) if kind == "src" else None
    result["files"].append(
        {"crate": crate, "file": rel, "kind": kind, "lines": len(lines), "test_module_from": cut}
    )
    for space, ty, impl_kind in _functions_of(tree, lines):
        fn = _function(space, rel, ty)
        fn["crate"] = crate
        in_test = kind == "tests" or (cut is not None and fn["line"] >= cut)
        result["tests" if in_test else "src"]["functions"].append(fn)
        if ty is not None and not in_test:
            result["type_counts"][(crate, ty, impl_kind)] += 1


def analyze() -> dict:
    """Measure every crate. Returns {'src': {...}, 'tests': {...}, 'files': [...]}."""
    result: dict = {
        "src": {"functions": [], "types": []},
        "tests": {"functions": []},
        "files": [],
        "type_counts": defaultdict(int),
    }
    for crate in crate_dirs():
        src = rs_files(crate, "src")
        tests = [f for d in TEST_DIRS for f in rs_files(crate, d)]
        for kind, files in (("src", src), ("tests", tests)):
            for path, tree in run_rca(files).items():
                _measure_file(path, tree, crate.name, kind, result)
    counts = result.pop("type_counts")
    result["src"]["types"] = sorted(
        ({"crate": c, "type": t, "impl": k, "methods": n} for (c, t, k), n in counts.items()),
        key=lambda t: (-t["methods"], t["crate"], t["type"]),
    )
    return result


def dist(xs: list[int], thresholds: tuple[int, ...]) -> dict:
    """Summary of a list of ints, the same fields py_baseline.py gives for Python."""
    s = sorted(xs)

    def pct(p: int) -> int:
        return s[max(0, -(-p * len(s) // 100) - 1)] if s else 0

    out = {
        "count": len(s),
        "max": s[-1] if s else 0,
        "p50": pct(50),
        "p90": pct(90),
        "p99": pct(99),
        "mean": round(sum(s) / len(s), 2) if s else 0,
    }
    for t in thresholds:
        out[f"over_{t}"] = sum(1 for x in s if x > t)
    return out
