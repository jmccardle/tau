"""Trace one representative turn across Rust and Python, and say where its time goes.

The turn: a user message, a `read` tool call (the Rust base tool) with every hook point
on, its result, and a final reply, through the test Harness with a FakeProvider. Run
twice: with the session in memory (`turn.json`) and on disk (`turn-stored.json`, where the
store's fsyncs show). Writes both Chrome traces and `summary.md` under `--out`
(default docs/as-built/traces). Open a trace in https://ui.perfetto.dev or chrome://tracing.

    .venv/bin/python scripts/trace_turn.py [--out DIR] [--runs N]

The Harness is the tests', so this imports it from tests/behavior.
"""

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "behavior"))

from fakes import FAKE_CONFIG, FakeClock, FakeProvider, Recorder, Reply, fake_catalog  # noqa: E402
from harness import Harness  # noqa: E402

from tau_next import Part, Session, Stop, base_tools, trace  # noqa: E402

TEXT = "violet-anchor-42\n"
KINDS = ("core", "store", "py", "hook", "sample", "tool", "driver")


async def make(root: Path, *, stored: bool) -> tuple[Harness, Path]:
    """A harness for the turn, with its workspace under `root`."""
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "secret.txt").write_text(TEXT)
    args = json.dumps({"path": "secret.txt"})
    provider = FakeProvider(
        [
            Reply([Part.Call("c1", "read", args)], Stop.Tools, finish_reason="tool_calls"),
            Reply([Part.Text("The phrase is violet-anchor-42")], finish_reason="stop"),
        ]
    )
    tools = base_tools(work, overflow_dir=root / "overflow")
    clock = FakeClock()
    directory = root / "session"
    session = Session.create(FAKE_CONFIG, "sys", clock.now(), directory if stored else None)
    return Harness(
        directory, session, provider, fake_catalog(), tools, [Recorder()], clock, []
    ), work


async def turn(root: Path, *, stored: bool, out: Path | None) -> float:
    """Run the turn; microseconds from the user message to quiet. Traced if `out`."""
    h, _ = await make(root, stored=stored)
    if out is not None:
        trace.start(out)
    began = time.perf_counter()
    await h.send("read it")
    await h.settle()
    wall = (time.perf_counter() - began) * 1e6
    if out is not None:
        trace.stop()
    *_, results, _final = h.bodies()
    if "violet-anchor-42" not in str(results):
        raise RuntimeError(f"the turn did not read the file: {results}")
    await h.close()
    return wall


@dataclass
class Node:
    name: str
    tid: int
    start: float
    dur: float
    children: float = 0.0
    parent: "Node | None" = field(default=None, repr=False)

    @property
    def kind(self) -> str:
        return self.name.split(".")[0] if "." in self.name else self.name

    @property
    def self_time(self) -> float:
        return self.dur - self.children


def spans(events: list[dict[str, Any]]) -> list[Node]:
    """Every span of a trace as a node with its parent's child time accounted."""
    nodes: list[Node] = []
    open_: dict[int, list[Node]] = defaultdict(list)
    for e in sorted((e for e in events if e["ph"] in "BEX"), key=lambda e: e["ts"]):
        tid = e["tid"]
        if e["ph"] == "E":
            node = open_[tid].pop()
            node.dur = e["ts"] - node.start
            if open_[tid]:
                open_[tid][-1].children += node.dur
            continue
        name = e["name"] if e["ph"] == "B" else f"{e['cat']}.{e['name']}"
        node = Node(name, tid, e["ts"], e.get("dur", 0.0))
        nodes.append(node)
        if e["ph"] == "B":
            open_[tid].append(node)
        else:  # complete events nest by containment
            stack = open_[tid]
            while stack and stack[-1].start + stack[-1].dur < node.start + node.dur:
                stack.pop()
            if stack:
                stack[-1].children += node.dur
            stack.append(node)
    claim_native(nodes)
    return nodes


NATIVE_HELPERS = ("py.lock", "py.machine", "py.save")


def claim_native(nodes: list[Node]) -> None:
    """Python and native spans sit on different tracks, so nothing nests them. A native
    call (`py.*`) made inside a Python span is that span's child: its time is not the
    span's own."""
    python = [n for n in nodes if n.kind in ("hook", "sample", "tool", "driver")]
    for call in (n for n in nodes if n.kind == "py" and n.name not in NATIVE_HELPERS):
        around = [p for p in python if inside(p, call)]
        if around:
            min(around, key=lambda p: p.dur).children += call.dur


def inside(outer: Node, inner: Node) -> bool:
    return outer.start <= inner.start and inner.start + inner.dur <= outer.start + outer.dur


def table(rows: list[tuple[str, ...]], header: tuple[str, ...]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


def by_name(nodes: list[Node]) -> list[str]:
    total: dict[str, list[float]] = defaultdict(list)
    for n in nodes:
        total[n.name].append(n.self_time)
    rows = [
        (f"`{name}`", str(len(t)), f"{sum(t):.1f}", f"{statistics.median(t):.1f}")
        for name, t in sorted(total.items(), key=lambda kv: -sum(kv[1]))
    ]
    return table(rows, ("span", "count", "self us", "median self us"))


def split(nodes: list[Node], wall: float) -> list[tuple[str, float]]:
    """Where the turn's wall time went. Self times are exclusive, so they add up;
    the driver's remainder is what no span covers (asyncio, hook plumbing)."""
    own: dict[str, float] = defaultdict(float)
    for n in nodes:
        if n.name.startswith("store.fsync"):
            own["store: fsync"] += n.self_time
        elif n.kind == "store":
            own["store: encode and write"] += n.self_time
        elif n.kind == "core":
            own["core"] += n.self_time
        elif n.kind == "py":
            own["bindings: native side (lock, conversion out, call)"] += n.self_time
        elif n.kind in ("hook", "sample", "tool"):
            own[f"effect: {n.kind} (fake or tool, as the driver waits on it)"] += n.self_time
        else:
            own["bindings: Python side (arguments in, call, results out)"] += n.self_time
    own["driver: unspanned (asyncio, hook plumbing, harness)"] = wall - sum(own.values())
    return sorted(own.items(), key=lambda kv: -kv[1])


def untraced(root: Path, *, stored: bool, runs: int) -> list[float]:
    def one() -> float:
        with tempfile.TemporaryDirectory(dir=root) as d:
            return asyncio.run(turn(Path(d), stored=stored, out=None))

    one()  # warm: imports, page cache
    return [one() for _ in range(runs)]


def report(label: str, path: Path, wall: float, plain: list[float]) -> list[str]:
    nodes = spans(json.loads(path.read_text()))
    lines = [f"## {label}", ""]
    lines += [
        f"Traced turn: {wall:.0f} us wall. Untraced, median of {len(plain)}: "
        f"{statistics.median(plain):.0f} us (min {min(plain):.0f}, max {max(plain):.0f}).",
        "",
        "### Where the turn's time goes",
        "",
    ]
    lines += table(
        [(k, f"{v:.0f}", f"{100 * v / wall:.1f}%") for k, v in split(nodes, wall)],
        ("part", "us", "share"),
    )
    lines += ["", "### Time per span (exclusive of children)", ""]
    lines += by_name(nodes)
    return [*lines, ""]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "as-built" / "traces")
    ap.add_argument("--runs", type=int, default=30, help="untraced runs for the baseline")
    opts = ap.parse_args()
    out: Path = opts.out
    out.mkdir(parents=True, exist_ok=True)
    lines = ["# Trace summary", "", "Generated by `scripts/trace_turn.py`.", ""]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for label, stored, name in (
            ("In memory", False, "turn.json"),
            ("Stored on disk", True, "turn-stored.json"),
        ):
            plain = untraced(root, stored=stored, runs=opts.runs)
            with tempfile.TemporaryDirectory(dir=root) as d:
                wall = asyncio.run(turn(Path(d), stored=stored, out=out / name))
            lines += report(label, out / name, wall, plain)
    (out / "summary.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
