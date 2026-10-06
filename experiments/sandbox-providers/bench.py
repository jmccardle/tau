"""Benchmark the code-mode sandbox providers. Prints a Markdown table.

    venv/bin/python experiments/sandbox-providers/bench.py [--repeat 50]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

from tau_agent_core.codemode.cpython import CPythonProvider
from tau_agent_core.codemode.invoker import DirectToolInvoker
from tau_agent_core.codemode.monty import MontyProvider
from tau_agent_core.codemode.protocol import RunLimits
from tau_agent_core.tools.base import AgentTool, ToolDefinition


async def _noop(tool_call_id: str, args: dict[str, Any], signal: Any = None) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(args.get("i", 0))}]}


NOOP = AgentTool(
    definition=ToolDefinition(
        name="noop",
        label="noop",
        description="Return i.",
        parameters={"type": "object", "properties": {"i": {"type": "integer"}}},
        execute=_noop,
    )
)
LIMITS = RunLimits(timeout_s=60, max_calls=5000)
SEQ = "for i in range(1000):\n    await tools.noop(i=i)"
GATHER = "import asyncio\nr = await asyncio.gather(*[tools.noop(i=i) for i in range(1000)])\nlen(r)"


def _make(name: str, lifetime: str, **kw: Any) -> Any:
    return (CPythonProvider if name == "cpython" else MontyProvider)(lifetime, **kw)


async def _time(p: Any, code: str, scope: str = "s") -> float:
    tools = DirectToolInvoker([NOOP], max_calls=5000)
    t = time.perf_counter()
    r = await p.run(code, tools=tools, limits=LIMITS, scope=scope)
    elapsed = (time.perf_counter() - t) * 1000
    assert r.ok, r.error
    return elapsed


async def bench(name: str, repeat: int) -> dict[str, float]:
    out: dict[str, float] = {}
    t = time.perf_counter()
    p = _make(name, "script", **({"prewarm": False} if name == "cpython" else {}))
    await p.start()
    await _time(p, "1")
    out["cold start + first run (ms)"] = (time.perf_counter() - t) * 1000
    await p.close()

    p = _make(name, "script")
    await p.start()
    await _time(p, "1")
    script = [await _time(p, "1") for _ in range(repeat)]
    out["warm run, lifetime=script, back-to-back (ms, median)"] = statistics.median(script)
    gapped = []
    for _ in range(min(repeat, 20)):
        await asyncio.sleep(0.25)
        gapped.append(await _time(p, "1"))
    out["warm run, lifetime=script, 250 ms apart (ms, median)"] = statistics.median(gapped)
    await p.close()

    p = _make(name, "session")
    await p.start()
    await _time(p, "1")
    session = [await _time(p, "1") for _ in range(repeat)]
    out["warm run, lifetime=session (ms, median)"] = statistics.median(session)
    one = statistics.median([await _time(p, "await tools.noop(i=1)") for _ in range(repeat)])
    out["run with 1 tool call, session (ms, median)"] = one
    seq = [await _time(p, SEQ) for _ in range(5)]
    out["1000 sequential tool calls (ms, median of 5)"] = statistics.median(seq)
    out["per sequential call (µs)"] = (statistics.median(seq) - statistics.median(session)) * 1000 / 1000
    gat = [await _time(p, GATHER) for _ in range(5)]
    out["1000 calls via gather (ms, median of 5)"] = statistics.median(gat)
    await p.close()
    return out


async def invoker_only(repeat: int = 20000) -> float:
    """µs per DirectToolInvoker.invoke with no sandbox at all."""
    tools = DirectToolInvoker([NOOP], max_calls=repeat + 1)
    t = time.perf_counter()
    for i in range(repeat):
        await tools.invoke("noop", {"i": i})
    return (time.perf_counter() - t) * 1e6 / repeat


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=50)
    args = parser.parse_args()
    results = {name: await bench(name, args.repeat) for name in ("cpython", "monty")}
    rows = list(results["cpython"])
    print("| measure | cpython | monty |\n|---|---:|---:|")
    for row in rows:
        print(f"| {row} | {results['cpython'][row]:.2f} | {results['monty'][row]:.2f} |")
    print(f"\ninvoker alone (validation + execute, no sandbox): {await invoker_only():.1f} µs/call")


if __name__ == "__main__":
    asyncio.run(main())
