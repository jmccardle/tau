"""Complexity and file-size distributions for a Python tree, via radon.

Used for the tau 0.15 baseline in docs/as-built/metrics (radon is not a
dependency of this repo; run it from a throwaway venv):

    git worktree add /tmp/tau-0.15 v0.15.0
    python3 -m venv /tmp/radonvenv && /tmp/radonvenv/bin/pip install radon==6.0.1
    /tmp/radonvenv/bin/python scripts/py_baseline.py /tmp/tau-0.15 \
        --src tau-llm/src tau-agent-core/src tau-coding-agent/src tau-jmfts/src \
        --tests tau-llm/tests tau-agent-core/tests tau-coding-agent/tests tau-jmfts/tests

Prints JSON: per group (src, tests) the distribution of function CC (methods
counted once) and of file line counts.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def pct(xs: list[int], p: float) -> int:
    """Nearest-rank percentile; 0 for an empty list."""
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, -(-int(p * len(xs)) // 100) - 1))]


def dist(xs: list[int], thresholds: tuple[int, ...]) -> dict[str, float | int]:
    """Summarize a list of ints."""
    out: dict[str, float | int] = {
        "count": len(xs),
        "max": max(xs, default=0),
        "p50": pct(xs, 50),
        "p90": pct(xs, 90),
        "p99": pct(xs, 99),
        "mean": round(sum(xs) / len(xs), 2) if xs else 0,
    }
    for t in thresholds:
        out[f"over_{t}"] = sum(1 for x in xs if x > t)
    return out


def functions(root: Path, trees: list[str]) -> list[dict]:
    """Every function/method once, with its CC."""
    p = subprocess.run(
        [Path(sys.executable).parent / "radon", "cc", "-j", "-s", *trees],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    out = []
    for path, blocks in json.loads(p.stdout).items():
        if isinstance(blocks, dict):
            raise SystemExit(f"radon error in {path}: {blocks}")
        for b in blocks:
            if b["type"] == "class":
                continue  # methods are listed alone as well; classes are not functions
            out.append(
                {
                    "file": path,
                    "name": b["name"],
                    "line": b["lineno"],
                    "cc": b["complexity"],
                    "loc": b["endline"] - b["lineno"] + 1,
                    "class": b.get("classname"),
                }
            )
    return out


def methods_per_class(root: Path, trees: list[str]) -> list[dict]:
    """Per-class method counts (radon lists them under the class)."""
    p = subprocess.run(
        [Path(sys.executable).parent / "radon", "cc", "-j", *trees],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        {"file": path, "name": b["name"], "line": b["lineno"], "methods": len(b.get("methods", []))}
        for path, blocks in json.loads(p.stdout).items()
        for b in blocks
        if b["type"] == "class"
    ]


def file_lines(root: Path, trees: list[str]) -> list[dict]:
    """Physical line count of every .py file."""
    return [
        {"file": str(f.relative_to(root)), "lines": len(f.read_text(errors="replace").splitlines())}
        for t in trees
        for f in sorted((root / t).rglob("*.py"))
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--src", nargs="+", required=True)
    ap.add_argument("--tests", nargs="+", required=True)
    a = ap.parse_args()
    res = {}
    for name, trees in (("src", a.src), ("tests", a.tests)):
        fn, fl, cl = (
            functions(a.root, trees),
            file_lines(a.root, trees),
            methods_per_class(a.root, trees),
        )
        res[name] = {
            "files": len(fl),
            "total_lines": sum(f["lines"] for f in fl),
            "cc": dist([f["cc"] for f in fn], (10, 15, 20, 40)),
            "function_loc": dist([f["loc"] for f in fn], (50, 100)),
            "file_lines": dist([f["lines"] for f in fl], (500, 800, 1500)),
            "methods_per_class": dist([c["methods"] for c in cl], (25,)),
            "top_cc": sorted(fn, key=lambda f: -f["cc"])[:15],
            "top_files": sorted(fl, key=lambda f: -f["lines"])[:10],
            "top_classes": sorted(cl, key=lambda c: -c["methods"])[:10],
        }
    json.dump(res, sys.stdout, indent=1)


main()
