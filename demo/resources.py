#!/usr/bin/env python3
"""What three τ terminal clients cost to show the same streamed output: CPU, instructions, memory.

    resources.py [--k 5] [--workloads stream,tools,open] [--takes instant,paced] [--force]

Clients (see demo/README.md, "Resources", for the process topology of each):
  0.15       the 0.15 TUI: one Python process.
  0.16       `tau`, the 0.16 Textual TUI: one Python process (an in-process client).
  ratatui    examples/ratatui: a Rust client of a Python `tau serve -d`: two processes.

Each runs in a 160x48 pane of a private tmux server against the fake model server, and is driven
by send-keys. The whole client-side process tree is sampled every 50 ms (the fake server, tmux
and this script are outside it). Writes demo/resources.md (and resources.json).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common
import scenario
from tmuxdrv import Tmux, shell_line

COLS, ROWS = 160, 48
TICK = 0.05  # sampling period
TIMEOUT = 600.0
IDLE_SECONDS = 30.0
SETTLE = 2.0  # after the open, before the idle window
CLK = os.sysconf("SC_CLK_TCK")
CLIENTS = ("0.15", "0.16", "ratatui")
RATATUI = common.HERE.parent / "examples" / "ratatui" / "target" / "release" / "trusty"
STREAM_CHARS = 16000  # ~4000 tokens (a token is four characters in the fake server)
TOOLS_ANSWER_CHARS = 4000  # ~1000 tokens
TOOL_CALLS = 20
MARK_STREAM = "RES-STREAM-DONE"
MARK_TOOLS = "RES-TOOLS-DONE"
PROMPT_STREAM = "Explain the pipeline in detail."
PROMPT_TOOLS = "Look at the first files and report."
LOAD_LIMIT = 2.0

# ---------------------------------------------------------------- the scripts

TOPICS = ("parse", "index", "merge", "scan", "cache", "render", "verify")


def section(i: int) -> str:
    topic = TOPICS[i % len(TOPICS)]
    out = [
        f"## Section {i}: the {topic} stage\n",
        f"The {topic} stage takes the output of stage {i - 1} and turns it into records the next "
        f"stage can use. It runs once per batch, keeps its working set small, and reports how "
        f"many items it saw ({i * 37 % 101} in this example), how many it kept "
        f"({i * 17 % 53}), and how long it took. A failure in this stage stops the batch.\n",
        f"- first, check the input of the {topic} stage ({i * 3 % 11} fields)",
        f"- second, apply the rule set {i % 5 + 1} to every record",
        "- third, write the result and its checksum",
        f"- last, hand over to stage {i + 1}\n",
        "```python",
        f"def {topic}_{i}(items, limit={i * 7 % 64 + 8}):",
        "    kept = []",
        "    for item in items:",
        f"        if item.size > limit and item.kind != '{topic}':",
        "            kept.append(item.name)",
        "    return sorted(kept)",
        "```\n",
    ]
    if i % 3 == 0:
        out += [
            "| stage | items | kept | seconds |",
            "|---|---|---|---|",
            *(
                f"| {topic}-{j} | {i * j * 13 % 997} | {i * j * 5 % 211} | {j}.{i % 10} |"
                for j in range(1, 5)
            ),
            "",
        ]
    return "\n".join(out) + "\n"


def stream_text(chars: int, marker: str) -> str:
    parts: list[str] = []
    size, i = 0, 0
    while size < chars - len(marker) - 2:
        i += 1
        parts.append(section(i))
        size += len(parts[-1])
    return "".join(parts) + marker


def script_stream() -> dict[str, Any]:
    return {"responses": [{"content": stream_text(STREAM_CHARS, MARK_STREAM)}]}


def script_tools() -> dict[str, Any]:
    responses: list[dict[str, Any]] = [
        {"content": None, "tool_calls": [scenario.read_call(i)]} for i in range(TOOL_CALLS)
    ]
    responses.append({"content": stream_text(TOOLS_ANSWER_CHARS, MARK_TOOLS)})
    return {"responses": responses}


@dataclass(frozen=True)
class Workload:
    name: str
    script: str
    prompt: str
    marker: str
    requests: int  # what the fake server must have been asked
    tokens: int  # displayed tokens of the answer

    @property
    def path(self) -> Path:
        return common.WORK / self.script


WORKLOADS = {
    "stream": Workload(
        "stream", "script-res-stream.json", PROMPT_STREAM, MARK_STREAM, 1, STREAM_CHARS // 4
    ),
    "tools": Workload(
        "tools",
        "script-res-tools.json",
        PROMPT_TOOLS,
        MARK_TOOLS,
        TOOL_CALLS + 1,
        TOOLS_ANSWER_CHARS // 4,
    ),
}


def prepare() -> None:
    scenario.prepare(common.WORK)
    (common.WORK / "script-res-stream.json").write_text(
        json.dumps(script_stream()), encoding="utf-8"
    )
    (common.WORK / "script-res-tools.json").write_text(json.dumps(script_tools()), encoding="utf-8")


# ---------------------------------------------------------------- /proc


def read_stat(pid: int) -> tuple[str, int]:
    """(comm, utime+stime in ticks) of a process."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    comm = raw[raw.index("(") + 1 : raw.rindex(")")]
    rest = raw[raw.rindex(")") + 2 :].split()
    return comm, int(rest[11]) + int(rest[12])


def read_mem(pid: int) -> tuple[int, int]:
    """(PSS, RSS) in kB from smaps_rollup."""
    pss = rss = 0
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        if line.startswith("Pss:"):
            pss = int(line.split()[1])
        elif line.startswith("Rss:"):
            rss = int(line.split()[1])
    return pss, rss


def children(pid: int) -> list[int]:
    out: list[int] = []
    for tid in os.listdir(f"/proc/{pid}/task"):
        text = Path(f"/proc/{pid}/task/{tid}/children").read_text()
        out += [int(x) for x in text.split()]
    return out


@dataclass
class Seen:
    role: str
    root: bool
    cpu: int = 0  # ticks, last seen
    pss: int = 0
    rss: int = 0
    alive: bool = True


@dataclass
class Sample:
    t: float
    cpu: dict[str, int]  # ticks by role
    pss: dict[str, int]
    rss: dict[str, int]


class Tree:
    """The client-side process tree: the roots (label, pid) and everything they spawn.

    A process that exits keeps the CPU it had when last seen (exited children are not lost
    unless they lived and died within one tick); its memory stops counting."""

    def __init__(self, roots: list[tuple[str, int]]) -> None:
        self.seen: dict[int, Seen] = {}
        self.roots = roots
        for label, pid in roots:
            self.seen[pid] = Seen(label, True)

    def sample(self) -> Sample:
        todo = [pid for _, pid in self.roots]
        live: set[int] = set()
        while todo:
            pid = todo.pop()
            live.add(pid)
            try:
                kids = children(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
            for k in kids:
                if k not in self.seen:
                    try:
                        comm, _ = read_stat(k)
                    except FileNotFoundError:
                        continue
                    self.seen[k] = Seen(f"{self.seen[pid].role}/{comm}", False)
                todo.append(k)
        cpu: dict[str, int] = {}
        pss: dict[str, int] = {}
        rss: dict[str, int] = {}
        for pid, s in self.seen.items():
            if s.alive:
                try:
                    s.cpu = read_stat(pid)[1]
                    s.pss, s.rss = read_mem(pid)
                except (FileNotFoundError, ProcessLookupError):
                    s.alive = False
                    s.pss = s.rss = 0
                    if s.root:
                        raise RuntimeError(f"client process {s.role} (pid {pid}) exited") from None
            cpu[s.role] = cpu.get(s.role, 0) + s.cpu
            pss[s.role] = pss.get(s.role, 0) + s.pss
            rss[s.role] = rss.get(s.role, 0) + s.rss
        return Sample(time.perf_counter(), cpu, pss, rss)


# ---------------------------------------------------------------- perf


def perf_status() -> tuple[bool, str]:
    """Whether `perf stat -e instructions:u` works for this user, and if not why and the fix."""
    if shutil.which("perf") is None:
        return (
            False,
            "`perf` is not installed (Debian/Ubuntu: `apt install linux-perf` / `linux-tools-generic`)",
        )
    r = subprocess.run(
        ["perf", "stat", "-x,", "-e", "instructions:u", "true"], capture_output=True, text=True
    )
    if r.returncode == 0 and "not supported" not in r.stderr and "<not" not in r.stderr:
        return True, "perf stat -e instructions:u"
    level = Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
    why = " ".join(r.stderr.split())[:200]
    return False, (
        f"`perf stat -e instructions:u` fails (perf_event_paranoid = {level}; perf says: {why}). "
        "Fix: `sudo sysctl kernel.perf_event_paranoid=1` (persist: a line "
        "`kernel.perf_event_paranoid = 1` in /etc/sysctl.d/99-perf.conf), or run with CAP_PERFMON. "
        "In a VM the host must also expose a virtual PMU (check with `perf stat -e instructions:u true` "
        "after the sysctl)"
    )


class Perf:
    """`perf stat -p PID` on each root, started at the window's beginning and stopped at its end."""

    def __init__(self, roots: list[tuple[str, int]]) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="perf-"))
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        for label, pid in roots:
            self.procs[label] = subprocess.Popen(
                [
                    "perf",
                    "stat",
                    "-x,",
                    "-e",
                    "instructions:u",
                    "-o",
                    str(self.dir / label),
                    "-p",
                    str(pid),
                ],
                stderr=subprocess.PIPE,
            )
        time.sleep(0.2)  # let the counters attach before the window starts
        for label, p in self.procs.items():
            if (code := p.poll()) is not None:
                raise RuntimeError(f"perf for {label} exited {code}: {p.stderr.read().decode()}")  # type: ignore[union-attr]

    def stop(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label, p in self.procs.items():
            p.send_signal(signal.SIGINT)
            p.wait(timeout=10)
            lines = [
                ln for ln in (self.dir / label).read_text().splitlines() if ln and ln[0] != "#"
            ]
            fields = lines[0].split(",") if lines else []
            if not fields or not fields[0].isdigit():
                raise RuntimeError(f"perf for {label} counted nothing: {lines!r}")
            out[label] = int(fields[0])
        shutil.rmtree(self.dir, ignore_errors=True)
        return out


# ---------------------------------------------------------------- one run


@dataclass
class Result:
    wall: float
    cpu: dict[str, float]  # seconds by role
    pss_peak: dict[str, int]  # kB by role (peak of each role) and "total" (peak of the sum)
    pss_final: dict[str, int]
    rss_peak_total: int
    rss_final_total: int
    out_bytes: int
    tmux_cpu: float
    instructions: dict[str, int] | None
    stats: dict[str, Any] = field(default_factory=dict)


def tmux_pid(tmux: Tmux) -> int:
    return int(tmux.run("display-message", "-p", "#{pid}").strip())


@dataclass
class Hooks:
    """What a window does besides sampling: `zero_base` counts CPU from the processes' start,
    `drive` sees every screen, `start` runs once the baseline is taken, `perf` counts instructions."""

    zero_base: bool = False
    drive: Any = None
    start: Any = None
    perf: bool = False


@dataclass
class Rig:
    """One run's client: its pane, the root processes, the tree, the pane's output file."""

    tmux: Tmux
    roots: list[tuple[str, int]]
    tree: Tree
    pipe: Path
    label: str
    perf_ok: bool

    def window(
        self, marker: str | None = None, seconds: float | None = None, hooks: Hooks | None = None
    ) -> Result:
        return window(self, marker, seconds, hooks or Hooks())


def window(rig: Rig, marker: str | None, seconds: float | None, hooks: Hooks) -> Result:
    """Sample the tree every TICK until `marker` is on screen or `seconds` have passed. The window's
    CPU and bytes are the difference from its start (zero when `zero_base`: the whole life of the
    processes); memory is the peak and the last sample."""
    tmux, roots, tree, pipe, label = rig.tmux, rig.roots, rig.tree, rig.pipe, rig.label
    perf, zero_base, drive, start = hooks.perf, hooks.zero_base, hooks.drive, hooks.start
    tmux_p = tmux_pid(tmux)
    counter = Perf(roots) if perf else None
    base = tree.sample()
    tmux0 = read_stat(tmux_p)[1]
    bytes0 = pipe.stat().st_size
    t0 = base.t
    samples = [base]
    if start:
        start()
    deadline = t0 + (seconds if seconds is not None else TIMEOUT)
    nxt = t0
    while True:
        nxt += TICK
        time.sleep(max(0.0, nxt - time.perf_counter()))
        try:
            s = tree.sample()
        except RuntimeError as e:
            raise RuntimeError(f"{label}: {e}; screen:\n{tmux.capture('r')}") from None
        samples.append(s)
        screen = tmux.capture("r")
        if drive:
            drive(screen)
        if marker and marker in screen:
            break
        if (gone := tmux.dead("r")) is not None:
            raise RuntimeError(f"{label}: the pane's program exited: {gone}")
        if seconds is not None and s.t >= deadline:
            break
        if marker and s.t >= deadline:
            raise TimeoutError(f"{label}: {marker!r} never appeared in {TIMEOUT:.0f}s:\n{screen}")
    end = samples[-1]
    instructions = counter.stop() if counter else None
    tmux1 = read_stat(tmux_p)[1]
    roles = list(end.cpu)
    start_cpu = {r: 0 for r in roles} if zero_base else base.cpu
    peak = {r: max(s.pss.get(r, 0) for s in samples) for r in roles}
    peak["total"] = max(sum(s.pss.values()) for s in samples)
    final = {r: end.pss.get(r, 0) for r in roles}
    final["total"] = sum(end.pss.values())
    return Result(
        wall=end.t - t0,
        cpu={r: (end.cpu[r] - start_cpu.get(r, 0)) / CLK for r in roles},
        pss_peak=peak,
        pss_final=final,
        rss_peak_total=max(sum(s.rss.values()) for s in samples),
        rss_final_total=sum(end.rss.values()),
        out_bytes=pipe.stat().st_size - bytes0,
        tmux_cpu=(tmux1 - tmux0) / CLK,
        instructions=instructions,
    )


def wait_stable(tmux: Tmux, quiet: float = 1.0, timeout: float = 60.0) -> None:
    """Block until the screen is non-empty and has not changed for `quiet` seconds."""
    last, since = "", time.perf_counter()
    deadline = since + timeout
    while True:
        screen = tmux.capture("r")
        now = time.perf_counter()
        if screen != last:
            last, since = screen, now
        elif screen.strip() and now - since >= quiet:
            return
        if now > deadline:
            raise TimeoutError(f"the screen never settled:\n{screen}")
        time.sleep(0.1)


def serve(session_args: list[str]) -> dict[str, Any]:
    r = subprocess.run(
        [str(common.tau_bin("16")), "serve", "-d", "--json", "-nc", *session_args],
        cwd=common.fixture_dir(),
        env=common.env_for("16"),
        capture_output=True,
        text=True,
    )
    if r.returncode:
        raise RuntimeError(f"tau serve -d exited {r.returncode}: {r.stdout}{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def stop_serve(record: dict[str, Any]) -> None:
    r = subprocess.run(
        [str(common.tau_bin("16")), "serve", "--stop", "--session", record["session"]],
        cwd=common.fixture_dir(),
        env=common.env_for("16"),
        capture_output=True,
        text=True,
    )
    if r.returncode:
        raise RuntimeError(f"tau serve --stop exited {r.returncode}: {r.stdout}{r.stderr}")
    for _ in range(100):
        if not Path(f"/proc/{record['pid']}").exists():
            return
        time.sleep(0.1)
    raise RuntimeError(f"the server (pid {record['pid']}) did not stop")


def sessions_of(version: str) -> Path:
    return common.sandbox(version) / ".tau" / "sessions"


def prepare_sessions(client: str, large: bool) -> None:
    version = "15" if client == "0.15" else "16"
    shutil.rmtree(sessions_of(version), ignore_errors=True)
    if large:
        src = common.sandbox(version) / ".tau" / "sessions.pristine"
        if not src.exists():
            raise SystemExit("no large session: run race.py build-b")
        shutil.copytree(src, sessions_of(version))


def run_once(client: str, name: str, take: str, perf_ok: bool) -> dict[str, Result]:
    """One run of `name` ("stream", "tools" or "open") on `client`: {cell: Result}. "open" gives
    the cells "open" and "idle"."""
    version = "15" if client == "0.15" else "16"
    large = name == "open"
    wl = WORKLOADS.get(name)
    script = wl.path if wl else common.WORK / "script-b.json"
    prepare_sessions(client, large)
    tmux = Tmux()
    record: dict[str, Any] | None = None
    pipe = Path(tempfile.mkdtemp(prefix="pane-")) / "out"
    pipe.touch()
    try:
        with common.fake_server(script, take == "paced") as port:
            common.write_config(version, port, "mocha")
            env = common.env_for(version)
            env["COLORTERM"] = "truecolor"
            if client == "0.15":
                argv = [str(common.tau_bin("15")), "--no-fun", "-nc"]
                argv += ["--resume"] if large else []
            elif client == "0.16":
                argv = [str(common.tau_bin("16")), "-nc"] + (["-c"] if large else [])
            else:
                record = serve(["-c"] if large else [])
                argv = [str(RATATUI), "--socket", record["socket"]]
            tmux.start(
                "-x", str(COLS), "-y", str(ROWS), "-s", "r",
                shell_line(env, argv, str(common.fixture_dir())),
            )  # fmt: skip
            tmux.run("pipe-pane", "-t", "r", f"cat >> {pipe}")
            roots = [
                (
                    "ui" if client == "ratatui" else "tau",
                    int(tmux.run("display-message", "-p", "-t", "r", "#{pane_pid}").strip()),
                )
            ]
            if record:
                roots.append(("server", int(record["pid"])))
            tree = Tree(roots)
            label = f"{client} {name}/{take}"
            rig = Rig(tmux, roots, tree, pipe, label, perf_ok)
            out = open_run(rig, client) if wl is None else stream_run(rig, wl)
            stats = common.server_stats(port)
            expect = wl.requests if wl else 0
            if stats["served"] != expect:
                raise RuntimeError(
                    f"{label}: the server saw {stats['served']} requests, expected {expect}"
                )
            for r in out.values():
                r.stats = stats
            return out
    finally:
        tmux.kill()
        if record:
            stop_serve(record)
        shutil.rmtree(pipe.parent, ignore_errors=True)


def stream_run(rig: Rig, wl: Workload) -> dict[str, Result]:
    wait_stable(rig.tmux)
    time.sleep(SETTLE)
    rig.tmux.run("send-keys", "-t", "r", "-l", wl.prompt)
    rig.tmux.wait_for("r", wl.prompt, 30.0)

    def go() -> None:
        rig.tmux.run("send-keys", "-t", "r", "Enter")

    return {wl.name: rig.window(wl.marker, hooks=Hooks(perf=rig.perf_ok, start=go))}


def open_run(rig: Rig, client: str) -> dict[str, Result]:
    pressed = False

    def drive(screen: str) -> None:
        nonlocal pressed
        if client == "0.15" and not pressed and common.PICKER_15 in screen:
            pressed = True
            rig.tmux.run("send-keys", "-t", "r", "Enter")

    opened = rig.window(scenario.FINISH_B, hooks=Hooks(zero_base=True, drive=drive))
    rig.window(seconds=SETTLE)  # memory settles; not reported
    idle = rig.window(seconds=IDLE_SECONDS, hooks=Hooks(perf=rig.perf_ok))
    opened.pss_final = idle.pss_final  # memory once settled, not at the first drawn line
    return {"open": opened, "idle": idle}


# ---------------------------------------------------------------- results


def med(values: list[float]) -> tuple[float, float, float]:
    return statistics.median(values), min(values), max(values)


def fmt(v: tuple[float, float, float], spec: str = ".2f") -> str:
    return f"{v[0]:{spec}} ({v[1]:{spec}}-{v[2]:{spec}})"


def totals(r: Result) -> float:
    return sum(r.cpu.values())


def instr_total(r: Result) -> float | None:
    return sum(r.instructions.values()) if r.instructions is not None else None


def load() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def cpu_model() -> tuple[str, int]:
    lines = Path("/proc/cpuinfo").read_text().splitlines()
    name = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("model name")), "?")
    return name, sum(1 for ln in lines if ln.startswith("processor"))


def check_stats(cells: dict[tuple[str, str], dict[str, list[Result]]]) -> list[str]:
    """Fail unless every client was sent the same dialogue in every run of a workload; return the
    table lines of what the server saw."""
    lines = [
        "| workload | requests | streamed | dialogue and tool calls | tool results | system prompt | tool schemas |",
        "|---|---|---|---|---|---|---|",
    ]
    for (wname, take), by_client in cells.items():
        if wname == "idle":
            continue
        allruns = [(c, r.stats) for c, rs in by_client.items() for r in rs]
        for c, s in allruns:
            if s["plain"] or s["streamed"] != s["served"]:
                raise SystemExit(f"{wname}/{take}: {c} sent requests without streaming: {s}")
        first = allruns[0][1]
        for c, s in allruns:
            if s["calls"] != first["calls"] or s["served"] != first["served"]:
                raise SystemExit(f"{wname}/{take}: {c} was sent a different dialogue")
        for c, rs in by_client.items():
            for key in ("results", "system", "tools"):
                if len({r.stats[key] for r in rs}) > 1:
                    raise SystemExit(f"{wname}/{take}: {c}'s own runs differ in {key}")
        cmp = [
            "same"
            if len({by_client[c][0].stats[key] for c in by_client}) == 1
            else "differs between 0.15 and 0.16"
            for key in ("results", "system", "tools")
        ]
        lines.append(
            f"| {wname}/{take} | {first['served']} | {first['streamed']} | same in all {len(allruns)} runs | "
            + " | ".join(cmp)
            + " |"
        )
    return [*lines, ""]


def client_row(
    c: str, rs: list[Result], ref: list[Result], tokens: int | None, perf_ok: bool
) -> list[str]:
    cpu = med([totals(r) for r in rs])
    cpu_ref = statistics.median(totals(r) for r in ref)
    row = [c, fmt(cpu), f"{cpu[0] / cpu_ref:.2f}x"]
    if perf_ok:
        ins = med([instr_total(r) / 1e9 for r in rs])  # type: ignore[operator]
        ref_ins = statistics.median(instr_total(r) / 1e9 for r in ref)  # type: ignore[operator]
        row += [fmt(ins, ".2f"), f"{ins[0] / ref_ins:.2f}x"]
    else:
        row += ["unavailable", "-"]
    row.append(f"{statistics.median(100 * totals(r) / r.wall for r in rs):.1f}%")
    if tokens:
        row.append(f"{statistics.median(1000 * totals(r) * 1000 / tokens for r in rs):.0f}")
    return row


def table(head: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
        *("| " + " | ".join(r) + " |" for r in rows),
        "",
    ]


TITLES = {
    "stream": "a. Stream: one ~4000-token markdown answer (code blocks, lists, a table)",
    "tools": f"b. Tool turn: {TOOL_CALLS} `read` calls, then a ~1000-token answer",
    "open": "d. Open: the 200-turn session opened (CPU from process start to the last turn drawn)",
    "idle": f"c. Idle: that session open, {IDLE_SECONDS:.0f} s with no input",
}


@dataclass(frozen=True)
class Meta:
    k: int
    perf: tuple[bool, str]
    load0: float
    load1: float
    forced: bool


def write_results(cells, meta: Meta) -> None:
    k, perf, load0, load1, forced = meta.k, meta.perf, meta.load0, meta.load1, meta.forced
    draft = k < 3 or max(load0, load1) > LOAD_LIMIT
    model, threads = cpu_model()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    ok, perf_msg = perf
    out = ["# Resource benchmark" + (" (DRAFT)" if draft else ""), ""]
    if draft:
        why = []
        if k < 3:
            why.append(f"K={k}")
        if max(load0, load1) > LOAD_LIMIT:
            why.append(
                f"load average {load0:.1f} before and {load1:.1f} after (limit {LOAD_LIMIT:g}"
                + (", run with --force" if forced else "")
                + ")"
            )
        out += [
            f"> **DRAFT: not a result** ({'; '.join(why)}). Rerun `demo/resources.py` with K>=5 on an idle machine.",
            "",
        ]
    out += [
        f"Taken {now}. Median (min-max) of {k} run(s) per client after one discarded warm-up run per client, "
        'clients rotating who goes first. See demo/README.md, "Resources", for the method and its limits.',
        "",
        "## Machine",
        "",
        f"- CPU: {model}, {threads} threads. Kernel {platform.release()}. {subprocess.run(['tmux', '-V'], capture_output=True, text=True).stdout.strip()}, pane {COLS}x{ROWS}.",
        f"- Load average (1 min): {load0:.2f} at start, {load1:.2f} at end",
        f"- Instructions: {'`' + perf_msg + '`, retired user-space instructions' if ok else 'UNAVAILABLE. ' + perf_msg}",
        "",
        "## Process topology",
        "",
        "| client | processes measured | roles in the tables |",
        "|---|---|---|",
        "| 0.15 | one Python process: driver, providers, tools and Textual UI together | `tau` |",
        "| 0.16 | one Python process: `tau` runs the session in-process, with Textual UI, driver and the Rust core (`tau._core`) in the same process | `tau` |",
        "| ratatui | two: `trusty` (Rust, the UI) and the Python `tau serve -d` daemon that runs the session and talks to the model | `ui`, `server` |",
        "",
        "Anything a process spawns is added to its role (as `role/comm`). Not measured: the fake model server, tmux, this script.",
        "",
    ]
    for (wname, take), by_client in cells.items():
        tokens = WORKLOADS[wname].tokens if wname in WORKLOADS else None
        take_text = f" ({take})" if take != "-" else ""
        out += [f"## {TITLES[wname]}{take_text}", ""]
        ref = by_client["0.15"]
        head = ["client", "CPU s", "vs 0.15", "instructions (G)", "vs 0.15", "% of one core"]
        if tokens:
            head.append("CPU ms per 1000 displayed tokens")
        out += table(head, [client_row(c, by_client[c], ref, tokens, ok) for c in CLIENTS])
        mem = []
        for c in CLIENTS:
            rs = by_client[c]
            mem.append([
                c,
                fmt(med([r.pss_peak["total"] / 1024 for r in rs]), ".1f"),
                fmt(med([r.pss_final["total"] / 1024 for r in rs]), ".1f"),
                f"{statistics.median(r.rss_final_total for r in rs) / 1024:.1f}",
                fmt(med([r.out_bytes / 1024 for r in rs]), ".0f"),
                f"{statistics.median(r.wall for r in rs):.1f}",
                f"{statistics.median(r.tmux_cpu for r in rs):.2f}",
            ])  # fmt: skip
        out += table(
            [
                "client",
                "peak PSS MiB",
                "final PSS MiB",
                "final RSS MiB",
                "bytes to terminal KiB",
                "wall s",
                "tmux server CPU s (not counted)",
            ],
            mem,
        )
        per = []
        for c in CLIENTS:
            rs = by_client[c]
            for role in sorted({x for r in rs for x in r.cpu}):
                per.append([
                    c,
                    role,
                    f"{statistics.median(r.cpu.get(role, 0) for r in rs):.2f}",
                    f"{statistics.median(r.pss_peak.get(role, 0) for r in rs) / 1024:.1f}",
                    f"{statistics.median(r.pss_final.get(role, 0) for r in rs) / 1024:.1f}",
                    f"{statistics.median((r.instructions or {}).get(role, 0) for r in rs) / 1e9:.2f}" if ok else "unavailable",
                ])  # fmt: skip
        out += ["Per process:", ""]
        out += table(
            ["client", "role", "CPU s", "peak PSS MiB", "final PSS MiB", "instructions (G)"], per
        )
    out += ["## What the fake server was sent", "", *check_stats(cells)]
    out += [
        "## Inherently unequal",
        "",
        "- ratatui prints plain text: it does not render markdown, highlight code, or fold tool calls. 0.15 and 0.16 do, and draw more. The three do not draw the same pixels, only show the same words.",
        "- ratatui has no theme and no session picker; the themes of 0.15 and 0.16 are both `mocha`.",
        "- 0.15 and 0.16 are separate programs with their own Textual and Rich versions.",
        "- ratatui's `server` role is started before the measured window; its startup cost is outside the stream and tool workloads and inside `open` (CPU from the process's start).",
        "- Instructions for `open` are not taken: perf attaches after a process starts, and the open happens at start.",
        "",
    ]
    (common.HERE / "resources.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    raw = {
        f"{w}/{t}/{c}": [
            {
                "wall": r.wall, "cpu": r.cpu, "pss_peak": r.pss_peak, "pss_final": r.pss_final,
                "rss_final": r.rss_final_total, "bytes": r.out_bytes, "tmux_cpu": r.tmux_cpu,
                "instructions": r.instructions,
            }
            for r in rs
        ]
        for (w, t), bc in cells.items()
        for c, rs in bc.items()
    }  # fmt: skip
    (common.HERE / "resources.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    print("\n".join(out))


def preflight(args: argparse.Namespace) -> tuple[float, tuple[bool, str]]:
    """The checks before anything runs: the client binary, the kernel, the load, perf."""
    if not RATATUI.exists():
        raise SystemExit(
            f"{RATATUI} is missing: cargo build --release --locked --manifest-path examples/ratatui/Cargo.toml"
        )
    if not Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").exists():
        raise SystemExit("this kernel has no /proc/PID/task/TID/children (CONFIG_PROC_CHILDREN)")
    load0 = load()
    if load0 > LOAD_LIMIT and not args.force:
        raise SystemExit(
            f"load average {load0:.1f} is over {LOAD_LIMIT:g}: wait, or pass --force (the result is marked DRAFT)"
        )
    perf = perf_status()
    if not perf[0]:
        print(f"WARNING: instruction counts unavailable: {perf[1]}", file=sys.stderr)
        if args.require_perf:
            raise SystemExit(1)
    return load0, perf


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--workloads", default="stream,tools,open")
    p.add_argument("--takes", default="instant,paced", help="for stream and tools")
    p.add_argument(
        "--force",
        action="store_true",
        help=f"run with a load average over {LOAD_LIMIT:g}, marked DRAFT",
    )
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument(
        "--require-perf", action="store_true", help="fail when instruction counts are unavailable"
    )
    args = p.parse_args()
    load0, perf = preflight(args)
    for v in common.VERSIONS:
        common.sandbox(v).mkdir(parents=True, exist_ok=True)
    prepare()
    plan: list[tuple[str, str]] = []
    for w in args.workloads.split(","):
        if w == "open":
            plan.append(("open", "-"))
        else:
            plan += [(w, t) for t in args.takes.split(",")]
    if not args.no_warmup:
        for c in CLIENTS:
            print(f"warm-up {c}", flush=True)
            run_once(c, "stream", "instant", perf[0])
    cells: dict[tuple[str, str], dict[str, list[Result]]] = {}
    for wname, take in plan:
        for i in range(args.k):
            order = CLIENTS[i % 3 :] + CLIENTS[: i % 3]
            for c in order:
                print(f"{wname}/{take}: run {i + 1}/{args.k} {c}", flush=True)
                for cell, res in run_once(c, wname, take, perf[0]).items():
                    cells.setdefault((cell, take), {}).setdefault(c, []).append(res)
    write_results(cells, Meta(args.k, perf, load0, load(), args.force))


if __name__ == "__main__":
    sys.exit(main())
