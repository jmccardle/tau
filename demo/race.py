#!/usr/bin/env python3
"""Headless races of τ 0.15 and τ 0.16 against the same fake model, and the results table.

    race.py prepare             write the fixture directory and the scripts
    race.py build-b             build the large session in each version (once; ~minutes)
    race.py bench [--k 5]       warm-up, then K timed runs per race, take and version -> results.md

Races (see demo/README.md for what each does and does not measure):
  a  one-shot `tau -p`: a prompt answered after CALLS_A sequential `read` calls.
  b  the TUI reopening (`tau -c`) a session of TURNS_B turns, in a tmux pane, until the last
     message is on screen ("drawn") and a typed key is echoed ("responsive"). The paced take then
     sends one more message and waits for its streamed answer ("answered").
Takes: `instant` (the fake model answers at once: harness overhead) and `paced` (it imitates a
real one: 400 ms to the first token, 70 tokens/s).
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import common
import scenario
from tmuxdrv import Tmux, choose_session, shell_line

COLS, ROWS = 160, 48
TIMEOUT = 600.0


def sessions(version: str):
    return common.sandbox(version) / ".tau" / "sessions"


def pristine(version: str):
    return common.sandbox(version) / ".tau" / "sessions.pristine"


# ---------------------------------------------------------------- race A


def run_print(
    version: str, script: str, words: list[str], paced: bool, *, cont: bool = False
) -> tuple[float, str]:
    """One `tau -p`: wall seconds, and the digest of the response indices the server served."""
    with common.fake_server(common.WORK / script, paced) as port:
        common.write_config(version, port, "mocha")
        t0 = time.perf_counter()
        r = subprocess.run(
            common.print_cmd(version, words, cont=cont),
            cwd=common.fixture_dir(),
            env=common.env_for(version),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        elapsed = time.perf_counter() - t0
        if r.returncode:
            raise SystemExit(f"tau {version} -p exited {r.returncode}:\n{r.stdout}\n{r.stderr}")
        return elapsed, str(common.server_stats(port))


def race_a(version: str, paced: bool) -> dict[str, float]:
    elapsed, stats = run_print(version, "script-a.json", [scenario.PROMPT_A], paced)
    expect = f"'served': {scenario.CALLS_A + 1},"
    if expect not in stats:
        raise SystemExit(
            f"{version}: the server saw {stats}, expected {scenario.CALLS_A + 1} requests"
        )
    return {"total": elapsed, "digest": stats.split("'digest': ")[1].strip("'}")}


def hello(version: str) -> dict[str, float]:
    elapsed, _ = run_print(version, "script-hello.json", ["hi"], False)
    return {"total": elapsed}


# ---------------------------------------------------------------- race B


def build_b(version: str) -> None:
    shutil.rmtree(sessions(version), ignore_errors=True)
    shutil.rmtree(pristine(version), ignore_errors=True)
    with common.fake_server(common.WORK / "script-b.json", False) as port:
        common.write_config(version, port, "mocha")
        for turn in range(scenario.TURNS_B):
            r = subprocess.run(
                common.print_cmd(version, [scenario.PROMPT_B], cont=turn > 0),
                cwd=common.fixture_dir(),
                env=common.env_for(version),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode:
                raise SystemExit(
                    f"{version} turn {turn}: exit {r.returncode}\n{r.stdout}\n{r.stderr}"
                )
            if turn % 25 == 0:
                print(f"  {version}: turn {turn + 1}/{scenario.TURNS_B}", flush=True)
    shutil.copytree(sessions(version), pristine(version))


def restore(version: str) -> None:
    shutil.rmtree(sessions(version), ignore_errors=True)
    shutil.copytree(pristine(version), sessions(version))


def reopen(version: str, paced: bool) -> dict[str, float]:
    """`tau -c` in a detached tmux pane: seconds until the last turn is drawn, until a typed
    message is echoed, and (paced) until the answer to that message has streamed in."""
    if not pristine(version).exists():
        raise SystemExit("no large session: run race.py build-b")
    restore(version)
    tmux = Tmux()
    try:
        with common.fake_server(common.WORK / "script-b.json", paced) as port:
            common.write_config(version, port, "mocha")
            env = common.env_for(version)
            env["COLORTERM"] = "truecolor"
            line = shell_line(env, common.tui_cmd(version), str(common.fixture_dir()))
            t0 = time.perf_counter()
            tmux.run("new-session", "-d", "-x", str(COLS), "-y", str(ROWS), "-s", "r", line)
            choose_session(tmux, "r", version)
            out = {"drawn": tmux.wait_for("r", scenario.FINISH_B, TIMEOUT) - t0}
            tmux.run("send-keys", "-t", "r", "-l", scenario.PROMPT_LIVE)
            out["responsive"] = tmux.wait_for("r", scenario.PROMPT_LIVE, TIMEOUT) - t0
            if paced:
                tmux.run("send-keys", "-t", "r", "Enter")
                out["answered"] = tmux.wait_for("r", scenario.FINISH_LIVE, TIMEOUT) - t0
            return out
    finally:
        tmux.kill()


# ---------------------------------------------------------------- bench


def stats(values: list[float]) -> tuple[float, float, float]:
    return statistics.median(values), min(values), max(values)


def machine() -> list[str]:
    cpu = next(
        (
            ln.split(":", 1)[1].strip()
            for ln in Path("/proc/cpuinfo").read_text().splitlines()
            if ln.startswith("model name")
        ),
        "?",
    )
    mem = next(
        (
            ln.split()[1]
            for ln in Path("/proc/meminfo").read_text().splitlines()
            if ln.startswith("MemTotal")
        ),
        "0",
    )
    versions = {
        v: subprocess.run(
            [str(common.tau_bin(v)), "--version"], capture_output=True, text=True
        ).stdout.strip()
        for v in common.VERSIONS
    }
    pys = {
        v: subprocess.run(
            [str(common.venv(v) / "bin" / "python"), "-V"], capture_output=True, text=True
        ).stdout.strip()
        for v in common.VERSIONS
    }
    return [
        f"- CPU: {cpu}, {len(open('/proc/cpuinfo').read().split('processor')) - 1} threads, {int(mem) // 1024 // 1024} GiB",
        f"- Kernel: {platform.release()} ({platform.machine()})",
        f"- 0.15: {versions['15']} on {pys['15']} (PyPI `ffwf-tau==0.15.0`)",
        f"- 0.16: {versions['16']} on {pys['16']} (release wheel built from this checkout)",
        f"- tmux {subprocess.run(['tmux', '-V'], capture_output=True, text=True).stdout.strip()}, TUI pane {COLS}x{ROWS}",
    ]


def load() -> float:
    return float(open("/proc/loadavg").read().split()[0])


def bench(k: int, races: list[str], takes: list[str]) -> None:
    for v in common.VERSIONS:
        common.sandbox(v).mkdir(parents=True, exist_ok=True)
    load0 = load()
    rows: dict[tuple[str, str, str], dict[str, list[float]]] = {}
    digests: set[str] = set()

    def record(race: str, take: str, version: str, result: dict[str, float]) -> None:
        cell = rows.setdefault((race, take, version), {})
        for key, val in result.items():
            if key == "digest":
                digests.add(str(val))
            else:
                cell.setdefault(key, []).append(val)

    base = {v: [hello(v)["total"] for _ in range(k + 1)][1:] for v in common.VERSIONS}
    for v in common.VERSIONS:
        rows[("startup", "instant", v)] = {"total": base[v]}

    for take in takes:
        paced = take == "paced"
        for race in races:
            fn = race_a if race == "a" else reopen
            for v in common.VERSIONS:  # warm-up, discarded
                fn(v, paced)
            for i in range(k):
                print(f"race {race} {take}: run {i + 1}/{k}", flush=True)
                for v in (
                    common.VERSIONS if i % 2 == 0 else common.VERSIONS[::-1]
                ):  # alternate who goes first
                    record(race, take, v, fn(v, paced))
    write_results(rows, k, digests, load0, load())


LABEL = {
    ("startup", "total"): "startup: `tau -p` one answer, no tools",
    ("a", "total"): "A: one-shot, {n} sequential `read` calls",
    ("b", "drawn"): "B: reopen {t}-turn session, last message drawn",
    ("b", "responsive"): "B: reopen, typed text echoed (responsive)",
    ("b", "answered"): "B: reopen, then a message streamed and answered",
}


def write_results(rows, k, digests, load0, load1) -> None:
    draft = max(load0, load1) > 2
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = ["# Race results" + (" (DRAFT)" if draft else ""), ""]
    if draft:
        out += [
            f"> **DRAFT: not a result.** The machine was under load (load average {load0:.1f} before, "
            f"{load1:.1f} after; the threshold is 2). Rerun `demo/race.py bench` on an idle machine.",
            "",
        ]
    out += [
        f"Taken {now}; median, min and max of {k} runs after one discarded warm-up run, seconds, "
        "versions alternating who goes first.",
        "",
        "## Machine",
        "",
        *machine(),
        f"- Load average (1 min): {load0:.2f} at start, {load1:.2f} at end",
        "",
    ]
    for take in ("instant", "paced"):
        keys = [key for key in rows if key[1] == take]
        if not keys:
            continue
        desc = (
            "the fake model answers at once, so this is the harness"
            if take == "instant"
            else "the fake model takes 400 ms to the first token and streams 70 tokens/s"
        )
        out += [
            f"## {take.capitalize()} take ({desc})",
            "",
            "| race | metric | 0.15 median (min-max) | 0.16 median (min-max) | 0.16 vs 0.15 |",
            "|---|---|---|---|---|",
        ]
        order = [
            ("startup", "total"),
            ("a", "total"),
            ("b", "drawn"),
            ("b", "responsive"),
            ("b", "answered"),
        ]
        for race, metric in order:
            c15 = rows.get((race, take, "15"), {}).get(metric)
            c16 = rows.get((race, take, "16"), {}).get(metric)
            if not (c15 and c16):
                continue
            (m15, lo15, hi15), (m16, lo16, hi16) = stats(c15), stats(c16)
            label = LABEL[(race, metric)].format(n=scenario.CALLS_A, t=scenario.TURNS_B)
            ratio = f"{m15 / m16:.2f}x faster" if m16 < m15 else f"{m16 / m15:.2f}x slower"
            out.append(
                f"| {race.upper()} | {label} | {m15:.2f} ({lo15:.2f}-{hi15:.2f}) | {m16:.2f} ({lo16:.2f}-{hi16:.2f}) | {ratio} |"
            )
        out.append("")
    out += [
        "Race A requests seen by the fake server were identical in both versions"
        + (
            f" (digest `{next(iter(digests))[:12]}`)."
            if len(digests) == 1
            else ": **NOT IDENTICAL**, digests " + ", ".join(sorted(digests))
        ),
        "",
    ]
    if len(digests) > 1:
        raise SystemExit("the versions saw different request sequences:\n" + "\n".join(out))
    (common.HERE / "results.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    (common.HERE / "results.json").write_text(
        json.dumps({f"{a}/{b}/{c}": v for (a, b, c), v in rows.items()}, indent=1), encoding="utf-8"
    )
    print("\n".join(out))


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    sub.add_parser("build-b")
    b = sub.add_parser("bench")
    b.add_argument("--k", type=int, default=5)
    b.add_argument("--races", default="a,b")
    b.add_argument("--takes", default="instant,paced")
    args = p.parse_args()
    if args.cmd == "prepare":
        print(scenario.prepare(common.WORK))
    elif args.cmd == "build-b":
        for v in common.VERSIONS:
            common.sandbox(v).mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(build_b, common.VERSIONS))
    else:
        scenario.prepare(common.WORK)
        bench(args.k, args.races.split(","), args.takes.split(","))


if __name__ == "__main__":
    sys.exit(main())
