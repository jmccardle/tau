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
from typing import Any

import common
import scenario
from tmuxdrv import Tmux, choose_session, shell_line

COLS, ROWS = 160, 48
TIMEOUT = 600.0


def sessions(version: str):
    return common.sandbox(version) / ".tau" / "sessions"


def pristine(version: str):
    return common.sandbox(version) / ".tau" / "sessions.pristine"


# What the fake server saw, per label ("A/instant", "B-build", "B-live") and version: the /_stats of
# each run. Compared across versions and runs at the end of a bench.
OBSERVED: dict[str, dict[str, list[dict[str, Any]]]] = {}


def observe(label: str, version: str, stats: dict[str, Any]) -> None:
    OBSERVED.setdefault(label, {}).setdefault(version, []).append(stats)


# ---------------------------------------------------------------- race A


def run_print(
    version: str, script: str, words: list[str], paced: bool, *, cont: bool = False
) -> tuple[float, str, dict[str, Any]]:
    """One `tau -p`: wall seconds, its standard output, and what the server saw."""
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
        return elapsed, r.stdout, common.server_stats(port)


def race_a(version: str, paced: bool) -> dict[str, float]:
    elapsed, out, stats = run_print(version, "script-a.json", [scenario.PROMPT_A], paced)
    if scenario.FINISH_A not in out:
        raise SystemExit(f"{version}: race A's answer lacks {scenario.FINISH_A!r}:\n{out}")
    if stats["served"] != scenario.CALLS_A + 1:
        raise SystemExit(f"{version}: the server saw {stats}, expected {scenario.CALLS_A + 1}")
    observe("A/" + ("paced" if paced else "instant"), version, stats)
    return {"total": elapsed}


def hello(version: str) -> dict[str, float]:
    elapsed, out, _ = run_print(version, "script-hello.json", ["hi"], False)
    if "hello" not in out:
        raise SystemExit(f"{version}: the startup run did not print the model's answer:\n{out}")
    return {"total": elapsed}


# ---------------------------------------------------------------- race B


def build_stats(version: str) -> Path:
    return common.WORK / f"build-b-{version}.json"


def build_b(version: str) -> None:
    """Run the scripted conversation through `version`; keep the session and what the server saw."""
    shutil.rmtree(sessions(version), ignore_errors=True)
    shutil.rmtree(pristine(version), ignore_errors=True)
    build_stats(version).unlink(missing_ok=True)
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
        if scenario.FINISH_B not in r.stdout:
            raise SystemExit(f"{version}: the last answer lacks {scenario.FINISH_B!r}:\n{r.stdout}")
        stats = common.server_stats(port)
    expect = scenario.TURNS_B * scenario.STEPS_PER_TURN
    if stats["served"] != expect:
        raise SystemExit(f"{version}: the server saw {stats}, expected {expect} requests")
    shutil.copytree(sessions(version), pristine(version))
    build_stats(version).write_text(json.dumps(stats), encoding="utf-8")


def restore(version: str) -> None:
    shutil.rmtree(sessions(version), ignore_errors=True)
    shutil.copytree(pristine(version), sessions(version))


def reopen(version: str, paced: bool) -> dict[str, float]:
    """The TUI reopening the large session in a detached tmux pane: seconds until the last turn
    is drawn, until a typed message is echoed, and (paced) until the answer to that message, sent
    after it, has streamed in."""
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
            tmux.start("-x", str(COLS), "-y", str(ROWS), "-s", "r", line)
            choose_session(tmux, "r", version)
            out = {"drawn": tmux.wait_for("r", scenario.FINISH_B, TIMEOUT) - t0}
            tmux.run("send-keys", "-t", "r", "-l", scenario.PROMPT_LIVE)
            out["responsive"] = tmux.wait_for("r", scenario.PROMPT_LIVE, TIMEOUT) - t0
            if paced:
                tmux.run("send-keys", "-t", "r", "Enter")
                out["answered"] = tmux.wait_for("r", scenario.FINISH_LIVE, TIMEOUT) - t0
                observe("B-live", version, common.server_stats(port))
            asked = common.server_stats(port)["served"]
            if asked != int(paced):
                raise SystemExit(f"{version}: reopening asked the model {asked} times")
            return out
    finally:
        tmux.kill()


# ---------------------------------------------------------------- bench


def stats(values: list[float]) -> tuple[float, float, float]:
    return statistics.median(values), min(values), max(values)


def packages(version: str) -> str:
    """The distribution's version, Python, Textual and Rich of one venv."""
    code = (
        "import importlib.metadata as m, sys;"
        "print('ffwf-tau', m.version('ffwf-tau'), '; Python', sys.version.split()[0],"
        " '; Textual', m.version('textual'), '; Rich', m.version('rich'))"
    )
    r = subprocess.run(
        [str(common.venv(version) / "bin" / "python"), "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def checkout() -> str:
    """The commit the 0.16 wheel was built from (the stamp setup.sh wrote), and the one the
    demo scripts are at, with whether either had uncommitted changes."""
    stamp = common.WORK / "wheels" / "COMMIT"
    built = (
        stamp.read_text().strip()
        if stamp.exists()
        else "unknown (no wheels/COMMIT: rerun setup.sh)"
    )
    here = subprocess.run(
        ["git", "-C", str(common.HERE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(common.HERE), "diff", "--quiet", "HEAD"]).returncode
    return f"wheel built from `{built}`; demo scripts at `{here}`" + (
        " plus uncommitted changes" if dirty else ""
    )


def machine() -> list[str]:
    cpuinfo = Path("/proc/cpuinfo").read_text().splitlines()
    cpu = next((ln.split(":", 1)[1].strip() for ln in cpuinfo if ln.startswith("model name")), "?")
    threads = sum(1 for ln in cpuinfo if ln.startswith("processor"))
    meminfo = Path("/proc/meminfo").read_text().splitlines()
    mem = next((ln.split()[1] for ln in meminfo if ln.startswith("MemTotal")), "0")
    tau = {
        v: subprocess.run(
            [str(common.tau_bin(v)), "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        for v in common.VERSIONS
    }
    tmux = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=True).stdout.strip()
    return [
        f"- CPU: {cpu}, {threads} threads, {int(mem) // 1024 // 1024} GiB",
        f"- Kernel: {platform.release()} ({platform.machine()})",
        f"- 0.15: `{tau['15']}`; {packages('15')} (PyPI)",
        f"- 0.16: `{tau['16']}`; {packages('16')} (release wheel)",
        f"- Source: {checkout()}",
        f"- {tmux}, TUI pane {COLS}x{ROWS}",
    ]


def load() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def order(i: int, offset: int) -> tuple[str, ...]:
    """Who goes first in round `i`: alternating, and flipped by `offset` so that an odd K does not
    always favour the same version across the cells of the table."""
    return common.VERSIONS if (i + offset) % 2 == 0 else common.VERSIONS[::-1]


def bench(k: int, races: list[str], takes: list[str]) -> None:
    for v in common.VERSIONS:
        common.sandbox(v).mkdir(parents=True, exist_ok=True)
    load0 = load()
    rows: dict[tuple[str, str, str], dict[str, list[float]]] = {}
    cell = 0  # the number of table cells timed so far: flips who goes first

    def record(race: str, take: str, version: str, result: dict[str, float]) -> None:
        for key, val in result.items():
            rows.setdefault((race, take, version), {}).setdefault(key, []).append(val)

    for i in range(k + 1):  # round 0 is the warm-up, discarded
        for v in order(i, cell):
            t = hello(v)
            if i:
                record("startup", "instant", v, t)
    cell += 1

    for take in takes:
        paced = take == "paced"
        for race in races:
            fn = race_a if race == "a" else reopen
            for v in order(0, cell):  # warm-up, discarded
                fn(v, paced)
            for i in range(k):
                print(f"race {race} {take}: run {i + 1}/{k}", flush=True)
                for v in order(i, cell):
                    record(race, take, v, fn(v, paced))
            cell += 1
    for v in common.VERSIONS:
        path = build_stats(v)
        if not path.exists():
            raise SystemExit(f"{path} is missing: rerun race.py build-b")
        observe("B-build", v, json.loads(path.read_text()))
    seen = compare_observed()
    write_results(rows, k, seen, load0, load())


LABEL = {
    ("startup", "total"): "startup: `tau -p` one answer, no tools",
    ("a", "total"): "A: one-shot, {n} sequential `read` calls",
    ("b", "drawn"): "B: reopen {t}-turn session, last message drawn",
    ("b", "responsive"): "B: reopen, typed text echoed (responsive)",
    ("b", "answered"): "B: reopen, then a message streamed and answered",
}

VIEWS = {
    "calls": "dialogue and tool calls",
    "results": "tool results",
    "system": "system prompt",
    "tools": "tool schemas",
}


def compare_observed() -> list[str]:
    """Compare what the server was sent. Fails when the dialogue or a tool call differs between
    versions or between runs of one version, when a client did not stream, or when a run's tool
    results differ between its own runs. Returns the lines for results.md: what matched, and
    what legitimately did not."""
    lines = [
        "## What the fake server was sent",
        "",
        "Each request is normalised and hashed, and the hashes are compared across the versions and "
        "across the runs of each. Normalised: message text loses trailing whitespace, a list of "
        "text parts is joined, tool-call arguments are parsed and re-serialised with sorted keys, "
        "tool-call ids are ignored. *Dialogue and tool calls*: every message's role and text, and "
        "every tool call's name and arguments (a mismatch fails the bench). *Tool results*: what "
        "each tool answered. *System prompt* and *tool schemas* are each version's own and are "
        "reported, not compared for equality. `B-build` is the 600 requests of building the "
        "session; `B-live` the one request of the live message after reopening it.",
        "",
        "| run | requests | streamed | " + " | ".join(VIEWS.values()) + " |",
        "|---|---|---|" + "---|" * len(VIEWS),
    ]
    for label in sorted(OBSERVED):
        by_version = OBSERVED[label]
        for v, runs in by_version.items():
            for r in runs:
                if r["plain"]:
                    raise SystemExit(
                        f"{label}: 0.{v} sent {r['plain']} request(s) without streaming"
                    )
            for key, what in VIEWS.items():
                if len({r[key] for r in runs}) > 1:
                    raise SystemExit(f"{label}: 0.{v}'s own runs differ in their {what}")
        first = {v: runs[0] for v, runs in by_version.items()}
        if len(first) != 2:
            continue
        a, b = first["15"], first["16"]
        if a["calls"] != b["calls"] or a["served"] != b["served"]:
            raise SystemExit(f"{label}: the versions were sent different dialogues or tool calls")
        cells = ["same" if a[key] == b[key] else "**differs**" for key in VIEWS]
        lines.append(
            f"| {label} | {a['served']} | {a['streamed']} of {a['served']} | "
            + " | ".join(cells)
            + " |"
        )
    lines.append("")
    return lines


def write_results(rows, k, seen, load0, load1) -> None:
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
        f"Taken {now}; median, min and max of {k} runs after one discarded warm-up round, seconds, "
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
        order_ = [
            ("startup", "total"),
            ("a", "total"),
            ("b", "drawn"),
            ("b", "responsive"),
            ("b", "answered"),
        ]
        for race, metric in order_:
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
    out += seen
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
