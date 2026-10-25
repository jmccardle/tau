"""Run the test suite under coverage and summarise per-module line/branch coverage.

Measures statements, missing lines, branch coverage, the largest uncovered
contiguous line ranges (with their enclosing function) per src module,
per-distribution totals, modules at 0% and below 50%, and the overlap with
the dead-code candidates written by ``deadcode.py``.

Usage:
    python scripts/arch/coverage_report.py [--out DIR] [--skip-run] [--deadcode FILE]
        [--pytest-args "..."]

Run from any directory with the interpreter that has pytest-cov. ``--skip-run``
reuses ``DIR/coverage.json`` and ``DIR/run.json``.

Outputs (in DIR, default ``build/arch/coverage``): ``coverage.json`` (raw),
``run.json`` (counts, wall time, failures), ``modules.json``, ``summary.md``,
``pytest.log``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from hyg_common import DISTRIBUTIONS, ROOT, dist_of, tool, write_json


def run_suite(out: Path, home: Path, extra: list[str]) -> dict:
    """Run pytest under coverage; return counts, wall time, and failing test ids."""
    tool("pytest_cov")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        str(ROOT / d / "src") for d in DISTRIBUTIONS if d != "tau-meta"
    )
    env["TAU_HOME"] = str(home / ".tau")
    env["HOME"] = str(home)
    (home / ".tau").mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfEs"]
    for m in ("tau_llm", "tau_agent_core", "tau_coding_agent", "tau_jmfts"):
        cmd.append(f"--cov={m}")
    cmd += ["--cov-branch", f"--cov-report=json:{out / 'coverage.json'}", *extra]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    wall = time.monotonic() - t0
    log = proc.stdout + "\n--- stderr ---\n" + proc.stderr
    (out / "pytest.log").write_text(log)
    last = [ln for ln in proc.stdout.splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
    counts = {
        k: int(n)
        for n, k in re.findall(
            r"(\d+) (passed|failed|skipped|deselected|errors?|xfailed|xpassed|warnings?)",
            last[-1] if last else "",
        )
    }
    failures = sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", proc.stdout, re.M)))
    return {
        "command": cmd[1:],
        "returncode": proc.returncode,
        "wall_seconds": round(wall, 1),
        "summary_line": last[-1] if last else "",
        "counts": counts,
        "failures": failures,
    }


def func_ranges(path: Path) -> list[tuple[int, int, str]]:
    """Return (start, end, qualified name) for every function in a source file."""
    tree = ast.parse(path.read_text())
    out: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        """Collect function spans with dotted qualified names."""
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + ch.name
                out.append((ch.lineno, ch.end_lineno or ch.lineno, q))
                walk(ch, q + ".")
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    return out


def uncovered_ranges(missing: list[int], executed: list[int]) -> list[tuple[int, int, int]]:
    """Group missing statements into runs not interrupted by an executed statement."""
    ex = sorted(executed)
    runs: list[tuple[int, int, int]] = []
    start = prev = None
    n = 0
    for ln in sorted(missing):
        if start is not None and not any(prev < e < ln for e in ex):
            prev = ln
            n += 1
            continue
        if start is not None:
            runs.append((start, prev, n))
        start = prev = ln
        n = 1
    if start is not None:
        runs.append((start, prev, n))
    return runs


def analyse(cov: dict) -> list[dict]:
    """Build the per-module records from a coverage.py JSON report."""
    mods = []
    for name, f in cov["files"].items():
        p = Path(name)
        rel = str(p.relative_to(ROOT)) if p.is_absolute() else name
        if "/src/" not in rel:
            continue
        s = f["summary"]
        funcs = func_ranges(ROOT / rel)
        runs = uncovered_ranges(f["missing_lines"], f["executed_lines"])
        runs.sort(key=lambda r: -r[2])
        top = []
        for a, b, n in runs[:5]:
            inside = [fn for fn in funcs if fn[0] <= a and b <= fn[1]]
            fn = min(inside, key=lambda x: x[1] - x[0])[2] if inside else "<module/class body>"
            top.append({"start": a, "end": b, "statements": n, "function": fn})
        br = s.get("num_branches", 0)
        mods.append(
            {
                "file": rel,
                "distribution": dist_of(rel),
                "statements": s["num_statements"],
                "missing": s["missing_lines"],
                "branches": br,
                "partial_branches": s.get("num_partial_branches", 0),
                "missing_branches": s.get("missing_branches", 0),
                "line_pct": round(
                    100 * (s["num_statements"] - s["missing_lines"]) / s["num_statements"], 1
                )
                if s["num_statements"]
                else 100.0,
                "combined_pct": round(s["percent_covered"], 1),
                "branch_pct": round(100 * (br - s.get("missing_branches", 0)) / br, 1)
                if br
                else None,
                "top_uncovered": top,
                "missing_lines": f["missing_lines"],
                "executed_lines": f["executed_lines"],
            }
        )
    return sorted(mods, key=lambda m: m["file"])


def dead_overlap(mods: list[dict], deadfile: Path) -> list[dict]:
    """Return deadcode candidates annotated with the share of their span's statements uncovered."""
    if not deadfile.exists():
        return []
    by = {m["file"]: m for m in mods}
    out = []
    for c in json.loads(deadfile.read_text())["vulture"]:
        m = by.get(c["file"])
        if m is None:
            continue
        lo, hi = c["span"]
        miss = sum(1 for ln in m["missing_lines"] if lo <= ln <= hi)
        exe = sum(1 for ln in m["executed_lines"] if lo <= ln <= hi)
        if miss and miss + exe:
            out.append(
                {
                    **c,
                    "span_statements": miss + exe,
                    "span_uncovered": miss,
                    "span_uncovered_pct": round(100 * miss / (miss + exe), 1),
                }
            )
    return sorted(out, key=lambda c: (-c["span_uncovered_pct"], c["file"], c["line"]))


def render(run: dict, mods: list[dict], overlap: list[dict]) -> str:
    """Render the markdown summary."""
    L = ["# Coverage summary", ""]
    L += [
        f"- command: `{' '.join(run['command'])}`",
        f"- return code: {run['returncode']}",
        f"- wall time: {run['wall_seconds']} s",
    ]
    L += [f"- pytest summary: `{run['summary_line']}`", f"- counts: {run['counts']}", ""]
    L += (
        [f"## Failing / erroring tests ({len(run['failures'])})", ""]
        + [f"- `{f}`" for f in run["failures"]]
        + [""]
    )
    L += [
        "## Per-distribution totals",
        "",
        "| distribution | modules | statements | missing | line % | branches | missing br. | combined % |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in DISTRIBUTIONS:
        ms = [m for m in mods if m["distribution"] == d]
        if not ms:
            continue
        st = sum(m["statements"] for m in ms)
        mi = sum(m["missing"] for m in ms)
        br = sum(m["branches"] for m in ms)
        mb = sum(m["missing_branches"] for m in ms)
        comb = 100 * (st + br - mi - mb) / (st + br) if st + br else 100
        L.append(
            f"| {d} | {len(ms)} | {st} | {mi} | {100 * (st - mi) / st if st else 100:.1f} | {br} | {mb} | {comb:.1f} |"
        )
    L += ["", "## Modules at 0% line coverage (with statements)", ""]
    L += [
        f"- `{m['file']}` ({m['statements']} stmts)"
        for m in mods
        if m["statements"] and m["line_pct"] == 0
    ]
    L += ["", "## Modules under 50% line coverage (excluding 0%)", ""]
    L += [
        f"- `{m['file']}`: {m['line_pct']}% ({m['missing']}/{m['statements']} missing)"
        for m in mods
        if 0 < m["line_pct"] < 50
    ]
    L += [
        "",
        "## Per-module table",
        "",
        "| file | stmts | miss | line % | branch % | largest uncovered range |",
        "|---|---|---|---|---|---|",
    ]
    for m in mods:
        t = m["top_uncovered"][0] if m["top_uncovered"] else None
        big = f"{t['start']}-{t['end']} ({t['statements']}) in `{t['function']}`" if t else ""
        bp = "" if m["branch_pct"] is None else m["branch_pct"]
        L.append(
            f"| {m['file']} | {m['statements']} | {m['missing']} | {m['line_pct']} | {bp} | {big} |"
        )
    L += [
        "",
        "## Dead-code candidates with uncovered statements in their span",
        "",
        f"{len(overlap)} vulture candidates have at least one uncovered statement in their span",
        "",
    ]
    if overlap:
        L += [
            "| file:line | qualname | kind | class | confidence | uncovered/statements |",
            "|---|---|---|---|---|---|",
        ]
        L += [
            f"| {c['file']}:{c['line']} | {c['qualname']} | {c['kind']} | {c['category']} | "
            f"{c['confidence']} | {c['span_uncovered']}/{c['span_statements']} "
            f"({c['span_uncovered_pct']}%) |"
            for c in overlap
        ]
    return "\n".join(L) + "\n"


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build/arch/coverage")
    ap.add_argument("--skip-run", action="store_true")
    ap.add_argument("--deadcode", type=Path, default=None)
    ap.add_argument(
        "--home", type=Path, default=None, help="HOME for the test run (default OUT/home)"
    )
    ap.add_argument("--pytest-args", default="")
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if a.skip_run:
        run = json.loads((out / "run.json").read_text())
    else:
        run = run_suite(out, (a.home or out / "home").resolve(), a.pytest_args.split())
        write_json(out / "run.json", run)
    cov = json.loads((out / "coverage.json").read_text())
    mods = analyse(cov)
    dead = a.deadcode or (out.parent / "deadcode" / "deadcode.json")
    overlap = dead_overlap(mods, dead)
    write_json(out / "modules.json", {"modules": mods, "dead_overlap": overlap})
    (out / "summary.md").write_text(render(run, mods, overlap))
    print(f"wrote {out / 'summary.md'}")


if __name__ == "__main__":
    main()
