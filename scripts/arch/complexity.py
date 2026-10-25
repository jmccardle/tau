"""Measure cyclomatic complexity, maintainability, size and class shape with radon.

Reports per-function CC and length, per-module MI and raw LOC/LLOC/comments,
the top functions and classes, counts of functions over CC 10/20/40 per
distribution, and the largest files with their share of each distribution.

Usage:
    python scripts/arch/complexity.py [--out DIR]

Outputs (in DIR, default ``build/arch/complexity``): ``functions.json``,
``classes.json``, ``modules.json``, ``summary.json``, ``summary.md``.
"""

from __future__ import annotations

import argparse
import json
import subprocess

from hyg_common import DISTRIBUTIONS, ROOT, SRC_TREES, dist_of, tool, write_json

THRESHOLDS = (10, 20, 40)


def radon(sub: str, *flags: str) -> dict:
    """Run ``radon <sub> -j`` over the five src trees and return the parsed JSON."""
    cmd = [*tool("radon"), sub, "-j", *flags, *SRC_TREES]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"radon {sub} failed: {p.stderr}")
    return json.loads(p.stdout)


def collect() -> tuple[list[dict], list[dict]]:
    """Return (function records, class records) from ``radon cc``."""
    funcs: list[dict] = []
    classes: list[dict] = []
    for path, blocks in radon("cc", "-s").items():
        if isinstance(blocks, dict):
            raise SystemExit(f"radon cc error in {path}: {blocks}")
        for b in blocks:
            if b["type"] == "class":
                classes.append(
                    {
                        "file": path,
                        "name": b["name"],
                        "line": b["lineno"],
                        "end": b["endline"],
                        "span": b["endline"] - b["lineno"] + 1,
                        "methods": len(b.get("methods", [])),
                        "distribution": dist_of(path),
                    }
                )
                items = b.get("methods", [])
            else:
                items = [b]
            for f in items:
                name = f["name"] if "classname" not in f else f"{f['classname']}.{f['name']}"
                funcs.append(
                    {
                        "file": path,
                        "name": name,
                        "line": f["lineno"],
                        "end": f["endline"],
                        "length": f["endline"] - f["lineno"] + 1,
                        "cc": f["complexity"],
                        "rank": f["rank"],
                        "distribution": dist_of(path),
                    }
                )
    uniq = {(f["file"], f["line"], f["name"]): f for f in funcs}
    key = lambda r: (r["file"], r["line"])  # noqa: E731
    return sorted(uniq.values(), key=key), sorted(classes, key=key)


def modules() -> list[dict]:
    """Return per-module MI plus raw LOC/LLOC/SLOC/comment counts."""
    mi = radon("mi", "-s")
    raw = radon("raw")
    out = []
    for path in sorted(raw):
        r = raw[path]
        m = mi.get(path, {})
        out.append(
            {
                "file": path,
                "distribution": dist_of(path),
                "loc": r["loc"],
                "lloc": r["lloc"],
                "sloc": r["sloc"],
                "comments": r["comments"],
                "multi": r["multi"],
                "blank": r["blank"],
                "mi": round(m.get("mi", 0), 2),
                "mi_rank": m.get("rank", ""),
            }
        )
    return out


def table(rows: list[dict], cols: list[str], head: list[str]) -> list[str]:
    """Render dict rows as markdown table lines."""
    L = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        L.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return L


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--out", type=__import__("pathlib").Path, default=ROOT / "build/arch/complexity"
    )
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    funcs, classes = collect()
    mods = modules()
    for f in funcs:
        f["where"] = f"{f['file']}:{f['line']}"
    for c in classes:
        c["where"] = f"{c['file']}:{c['line']}"
    write_json(a.out / "functions.json", funcs)
    write_json(a.out / "classes.json", classes)
    write_json(a.out / "modules.json", mods)
    top_cc = sorted(funcs, key=lambda r: (-r["cc"], r["where"]))[:30]
    top_len = sorted(funcs, key=lambda r: (-r["length"], r["where"]))[:20]
    top_methods = sorted(classes, key=lambda r: (-r["methods"], r["where"]))[:20]
    top_span = sorted(classes, key=lambda r: (-r["span"], r["where"]))[:20]
    over = {
        d: {
            str(t): sum(1 for f in funcs if f["distribution"] == d and f["cc"] > t)
            for t in THRESHOLDS
        }
        for d in DISTRIBUTIONS
    }
    totals = {}
    for d in DISTRIBUTIONS:
        ms = [m for m in mods if m["distribution"] == d]
        fs = [f for f in funcs if f["distribution"] == d]
        totals[d] = {
            "files": len(ms),
            "loc": sum(m["loc"] for m in ms),
            "lloc": sum(m["lloc"] for m in ms),
            "sloc": sum(m["sloc"] for m in ms),
            "comments": sum(m["comments"] for m in ms),
            "functions": len(fs),
            "mean_cc": round(sum(f["cc"] for f in fs) / len(fs), 2) if fs else 0,
        }
    big = []
    for m in sorted(mods, key=lambda m: (-m["loc"], m["file"]))[:25]:
        big.append({**m, "share_pct": round(100 * m["loc"] / totals[m["distribution"]]["loc"], 1)})
    write_json(
        a.out / "summary.json",
        {
            "over_cc": over,
            "totals": totals,
            "top_cc": top_cc,
            "top_len": top_len,
            "top_methods": top_methods,
            "top_span": top_span,
            "largest_files": big,
        },
    )
    L = ["# Complexity summary", "", "## Per-distribution totals", ""]
    L += table(
        [
            {"d": d, **t, "o10": over[d]["10"], "o20": over[d]["20"], "o40": over[d]["40"]}
            for d, t in totals.items()
        ],
        [
            "d",
            "files",
            "loc",
            "lloc",
            "sloc",
            "comments",
            "functions",
            "mean_cc",
            "o10",
            "o20",
            "o40",
        ],
        [
            "distribution",
            "files",
            "LOC",
            "LLOC",
            "SLOC",
            "comments",
            "functions",
            "mean CC",
            "CC>10",
            "CC>20",
            "CC>40",
        ],
    )
    L += ["", "## Top 30 functions by cyclomatic complexity", ""]
    L += table(
        top_cc,
        ["where", "name", "cc", "rank", "length"],
        ["file:line", "function", "CC", "rank", "lines"],
    )
    L += ["", "## Top 20 functions by length", ""]
    L += table(top_len, ["where", "name", "length", "cc"], ["file:line", "function", "lines", "CC"])
    L += ["", "## Top 20 classes by method count", ""]
    L += table(
        top_methods,
        ["where", "name", "methods", "span"],
        ["file:line", "class", "methods", "line span"],
    )
    L += ["", "## Top 20 classes by line span", ""]
    L += table(
        top_span,
        ["where", "name", "span", "methods"],
        ["file:line", "class", "line span", "methods"],
    )
    L += ["", "## Largest 25 files (LOC) and share of their distribution", ""]
    L += table(
        big,
        ["file", "loc", "lloc", "share_pct", "mi", "mi_rank"],
        ["file", "LOC", "LLOC", "% of dist.", "MI", "MI rank"],
    )
    low = sorted(mods, key=lambda m: (m["mi"], m["file"]))[:20]
    L += ["", "## Lowest 20 maintainability indexes", ""]
    L += table(low, ["file", "mi", "mi_rank", "loc"], ["file", "MI", "rank", "LOC"])
    (a.out / "summary.md").write_text("\n".join(L) + "\n")
    print(f"wrote {a.out / 'summary.md'}")


if __name__ == "__main__":
    main()
