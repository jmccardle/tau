"""Measure the stateful-sandbox strategies (experiment, 2026-10-05).

Usage: ``python measure.py OUT.json``. Writes every number RESULTS.md quotes and
prints the tables. Timings are medians of ``REPEAT`` runs on one warm pool.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import pydantic_monty as monty  # noqa: E402
from cpython_alt import (  # noqa: E402
    ReplayDivergence,
    pickle_namespace,
    replay,
    run_recording,
    timed,
    unpickle_namespace,
)
from sandbox_state import BlobStore, MontySandbox  # noqa: E402
from tau_agent_core.agent_session import AgentSession  # noqa: E402
from tau_coding_agent.session_store import Session  # noqa: E402
from tau_llm.types import Model  # noqa: E402

REPEAT = 7

STATES = {
    "empty": "pass",
    "rows_1k": "rows = {}\nfor i in range(1000):\n    rows[i] = {'id': i, 'name': 'user' + str(i), 'score': i * 1.5, 'tags': ['a', 'b']}",
    "functions_5": (
        "def add(a, b):\n    return a + b\n"
        "def mean(xs):\n    return sum(xs) / len(xs)\n"
        "def top(d, n):\n    return sorted(d.values(), key=lambda r: r['score'])[-n:]\n"
        "def norm(s):\n    return s.strip().lower()\n"
        "def fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n"
    ),
    "str_1MiB": "big = 'x' * (1024 * 1024)",
    "random_200k": "import random\nrandom.seed(1)\nnoise = ''.join([chr(97 + random.randint(0, 25)) for _ in range(200000)])",
}
STATES["combined"] = STATES["rows_1k"] + "\n" + STATES["functions_5"] + STATES["str_1MiB"]


def _model() -> Model:
    return Model(
        id="m", provider="openai", api="openai-completions",
        base_url="http://127.0.0.1:1/v1", name="m", context_window=8192, max_tokens=256,
    )


def med(xs: list[float]) -> float:
    return round(statistics.median(xs), 3)


async def monty_states(pool: monty.AsyncMonty) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, code in STATES.items():
        dump_ms, load_ms = [], []
        raw = b""
        for _ in range(REPEAT):
            async with pool.checkout() as s:
                await s.feed_run(code)
                t = time.perf_counter()
                raw = await s.dump()
                dump_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            async with pool.checkout() as s2:
                await s2.load_session(raw)
                await s2.feed_run("1")
                load_ms.append((time.perf_counter() - t) * 1000)
        import zlib

        z = zlib.compress(raw, 6)
        out[label] = {
            "raw_bytes": len(raw),
            "zlib_bytes": len(z),
            "dump_ms": med(dump_ms),
            "checkout_load_ms": med(load_ms),
        }
    return out


async def monty_runs(pool: monty.AsyncMonty, tmp: Path) -> dict[str, Any]:
    """Per-script overhead and session-file growth through MontySandbox on a real file Session."""
    out: dict[str, Any] = {}
    for label, code in STATES.items():
        log = Session.create(str(tmp), "m", "openai", base_dir=tmp / f"s-{label}")
        assert log.path is not None
        session = AgentSession(session_log=log, model=_model(), tools=[])
        box = MontySandbox(pool, blobs=BlobStore.beside(log.path))
        before = log.path.stat().st_size
        r = await box.run(session.cursor, code)
        grew = log.path.stat().st_size - before
        blob_dir = BlobStore.beside(log.path).root
        blob = sum(p.stat().st_size for p in blob_dir.glob("*")) if blob_dir.exists() else 0
        hit = await box.run(session.cursor, "1")
        await box.close()
        miss = await box.run(session.cursor, "1")
        await box.close()
        out[label] = {
            "entry_line_bytes": grew,
            "side_file_bytes": blob,
            "listing": {n: v["type"] for n, v in r.listing.items()},
            "first_run": {k: round(v, 3) for k, v in r.timings.items()},
            "cache_hit_restore_ms": round(hit.timings["restore_ms"], 3),
            "cache_miss_restore_ms": round(miss.timings["restore_ms"], 3),
            "overhead_ms_hit": round(sum(v for k, v in hit.timings.items() if k != "run_ms"), 3),
        }
    return out


async def growth(pool: monty.AsyncMonty, tmp: Path) -> dict[str, Any]:
    """Session-file growth when every script adds 100 rows to one growing table."""
    log = Session.create(str(tmp), "m", "openai", base_dir=tmp / "growth")
    assert log.path is not None
    session = AgentSession(session_log=log, model=_model(), tools=[])
    box = MontySandbox(pool, blobs=BlobStore.beside(log.path))
    await box.run(session.cursor, "rows = []")
    sizes = []
    lines = []
    for i in range(20):
        before = log.path.stat().st_size
        await box.run(
            session.cursor,
            f"for j in range(100):\n    rows.append({{'id': {i} * 100 + j, 'name': 'n' + str(j), 'v': j * 0.5}})",
        )
        lines.append(log.path.stat().st_size - before)
        sizes.append(log.path.stat().st_size)
    await box.close()
    small = Session.create(str(tmp), "m", "openai", base_dir=tmp / "small")
    assert small.path is not None
    s2 = AgentSession(session_log=small, model=_model(), tools=[])
    box2 = MontySandbox(pool, blobs=BlobStore.beside(small.path))
    small_lines = []
    for i in range(20):
        before = small.path.stat().st_size
        await box2.run(s2.cursor, f"v{i} = {i} * 2")
        small_lines.append(small.path.stat().st_size - before)
    await box2.close()
    return {
        "growing_table_entry_bytes": lines,
        "growing_table_file_bytes": sizes,
        "small_vars_entry_bytes": small_lines,
    }


async def delta_compression(pool: monty.AsyncMonty) -> dict[str, Any]:
    """Bytes stored for the growing-table run as full snapshots vs. lzma deltas against the parent."""
    import lzma
    import zlib

    async with pool.checkout() as s:
        await s.feed_run("rows = []")
        prev = await s.dump()
        full_z = full_x = delta_x = 0
        for i in range(20):
            await s.feed_run(
                f"for j in range(100):\n    rows.append({{'id': {i} * 100 + j, 'name': 'n' + str(j), 'v': j * 0.5}})"
            )
            cur = await s.dump()
            full_z += len(zlib.compress(cur, 6))
            full_x += len(lzma.compress(cur))
            delta_x += len(lzma.compress(prev + cur)) - len(lzma.compress(prev))
            prev = cur
    return {"sum_zlib_full": full_z, "sum_lzma_full": full_x, "sum_lzma_delta_vs_parent": delta_x}


async def dead_future(pool: monty.AsyncMonty) -> dict[str, Any]:
    """An un-awaited async host call stored in a variable, dumped and restored."""

    async def afetch(u: str) -> str:
        return "body:" + u

    def err(exc: BaseException) -> str:
        return f"{type(exc).__name__}: {str(exc)[:160]}"

    res: dict[str, Any] = {}
    try:
        async with pool.checkout() as s:
            await s.feed_run("pending = afetch('u')", external_lookup={"afetch": afetch})
            res["stored_repr"] = await s.feed_run("repr(pending)")
            try:
                res["await_next_feed"] = await s.feed_run("await pending", external_lookup={"afetch": afetch})
            except Exception as exc:
                res["await_next_feed"] = err(exc)
            try:
                res["session_after"] = await s.feed_run("1 + 1")
            except Exception as exc:
                res["session_after"] = err(exc)
    except Exception as exc:
        res["checkout_exit"] = err(exc)
    async with pool.checkout() as s:
        await s.feed_run("pending2 = afetch('v')", external_lookup={"afetch": afetch})
        raw = await s.dump()
    try:
        async with pool.checkout() as s2:
            await s2.load_session(raw)
            try:
                res["await_after_restore"] = await s2.feed_run("await pending2", external_lookup={"afetch": afetch})
            except Exception as exc:
                res["await_after_restore"] = err(exc)
    except Exception as exc:
        res["restore_checkout_exit"] = err(exc)
    return res


def cpython_states() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, code in STATES.items():
        ns: dict[str, Any] = {"__name__": "__sandbox__"}
        exec(compile(code, "<s>", "exec"), ns)
        row: dict[str, Any] = {}
        for lib in ("dill", "cloudpickle"):
            times, loads = [], []
            data = b""
            failed: dict[str, str] = {}
            for _ in range(REPEAT):
                (data, failed), ms = timed(pickle_namespace, ns, lib)
                times.append(ms)
                if data is not None:
                    _, lms = timed(unpickle_namespace, data)
                    loads.append(lms)
            import zlib

            row[lib] = {
                "bytes": 0 if data is None else len(data),
                "zlib_bytes": 0 if data is None else len(zlib.compress(data, 6)),
                "dump_ms": med(times),
                "load_ms": med(loads) if loads else None,
                "failed": failed,
            }
        out[label] = row
    return out


def cpython_breakage() -> dict[str, Any]:
    """Which namespace values each pickler refuses."""

    class Session:  # stands in for a host object a tool closure captures
        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.sock = socket.socket()

        def fetch(self, url: str) -> str:
            return url

    host = Session()
    script_class = "class Point:\n    def __init__(self, x):\n        self.x = x\np = Point(3)\n"
    ns: dict[str, Any] = {"__name__": "__sandbox__"}
    exec(compile(script_class, "<s>", "exec"), ns)
    ns.update(
        {
            "fh": open(__file__),
            "gen": (i for i in range(3)),
            "lock": threading.Lock(),
            "sock": socket.socket(),
            "mod_json": json,
            "lam": lambda v: v + 1,
            "buf": io.StringIO("abc"),
            "tool": host.fetch,
        }
    )
    out: dict[str, Any] = {}
    for lib in ("dill", "cloudpickle"):
        _, failed = pickle_namespace(ns, lib)
        out[lib] = failed
    ns["fh"].close()
    ns["sock"].close()
    host.sock.close()
    return out


def cpython_replay() -> dict[str, Any]:
    tools = {"fetch": lambda u: {"url": u, "len": len(u)}}
    scripts = [STATES["rows_1k"], STATES["functions_5"], "page = fetch('https://a')\nscore = mean([r['score'] for r in rows.values()])"]
    scripts += [f"acc_{i} = fib(15) + {i}" for i in range(7)]
    ns, records = run_recording(scripts, tools)
    times = [timed(replay, scripts, records)[1] for _ in range(REPEAT)]
    heavy = ["total = 0\nfor i in range(2_000_000):\n    total += i"]
    _, heavy_records = run_recording(heavy, {})
    heavy_ms = med([timed(replay, heavy, heavy_records)[1] for _ in range(3)])
    rand = ["import random\nk = random.random()\nr = fetch(str(k))"]
    _, rand_records = run_recording(rand, tools)
    try:
        replay(rand, rand_records)
        detected = "not detected"
    except ReplayDivergence as exc:
        detected = f"ReplayDivergence: {str(exc)[:100]}"
    silent = ["import random\nk = random.random()"]
    first, rec = run_recording(silent, {})
    again = replay(silent, rec)
    return {
        "scripts": len(scripts),
        "replay_ms": med(times),
        "heavy_loop_2e6_replay_ms": heavy_ms,
        "random_reaching_a_tool": detected,
        "random_not_reaching_a_tool_same_value": first["k"] == again["k"],
        "recorded_bytes": len(json.dumps({"scripts": scripts, "records": records})),
    }


async def main(out_path: Path) -> None:
    results: dict[str, Any] = {"monty_version": monty.__version__, "python": sys.version.split()[0]}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        async with monty.AsyncMonty(min_processes=2) as pool:
            async with pool.checkout() as warm:
                await warm.feed_run("1")
            results["monty_states"] = await monty_states(pool)
            results["monty_runs"] = await monty_runs(pool, tmp)
            results["growth"] = await growth(pool, tmp)
            results["delta_compression"] = await delta_compression(pool)
            results["dead_future"] = await dead_future(pool)
    results["cpython_states"] = cpython_states()
    results["cpython_breakage"] = cpython_breakage()
    results["cpython_replay"] = cpython_replay()
    out_path.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
