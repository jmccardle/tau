"""Stress: N jobs complete while the head's turn is aborted/rolled back; count deliveries per handle.

Run from the repo root. ``old`` loads the pre-2026-10-05 module from a file:

    git show dd0595a:tau-agent-core/src/tau_agent_core/extensions_builtin/background.py > OLD.py
    venv/bin/python experiments/background-cursors/stress.py old 500 12 OLD.py
    venv/bin/python experiments/background-cursors/stress.py new 500 12
"""
import asyncio
import collections
import importlib.util
import random
import sys
import time
from unittest.mock import patch

sys.path.insert(0, "tau-agent-core/tests")
import test_background_cursors as T  # noqa: E402

which, iters, jobs = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
if which == "old":
    spec = importlib.util.spec_from_file_location("background_old", sys.argv[4])
    old = importlib.util.module_from_spec(spec)
    sys.modules["background_old"] = old
    spec.loader.exec_module(old)
    T.bg = old


async def once(seed):
    rng = random.Random(seed)
    rig = T._Rig()
    head = rig.session.cursor
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=rig.script.stream):
        handles = []
        for i in range(jobs):
            out = await rig.tool("slow").execute(f"c{i}", {"q": f"q{i}", T.bg.BACKGROUND_KEY: True})
            handles.append(out["details"]["background_handle"])
        rig.script.hold = asyncio.Event()
        turn = asyncio.create_task(rig.submit("hello"))
        await T._until(lambda: head.busy)

        async def complete(i):
            for _ in range(rng.randint(0, 40)):
                await asyncio.sleep(0)
            rig.gate(f"q{i}").set()

        mode = "abort" if rng.random() < 0.5 else "rollback"

        async def interrupt():
            for _ in range(rng.randint(0, 30)):
                await asyncio.sleep(0)
            if mode == "abort":
                rig.session.abort(head)
                for _ in range(rng.randint(0, 5)):
                    await asyncio.sleep(0)
                rig.script.hold.set()
                rig.script.hold = None
            else:
                rb = asyncio.create_task(rig.submit("instead", strategy="rollback"))
                await asyncio.sleep(0)
                rig.script.hold.set()
                rig.script.hold = None
                await rb

        await asyncio.gather(*(complete(i) for i in range(jobs)), interrupt())
        await turn
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while (rig.registry._tasks or head.busy) and loop.time() < deadline:
            await asyncio.sleep(0.001)
        for _ in range(50):
            await asyncio.sleep(0.001)
        if head.next_turn_queue:
            await rig.submit("flush")
        stranded = len(head.steer_queue)
    counts = collections.Counter(T._count(head, h) for h in handles)
    return mode, counts, stranded


async def main():
    tot = collections.Counter()
    by_mode = collections.defaultdict(collections.Counter)
    strand = 0
    t0 = time.time()
    for s in range(iters):
        mode, c, st = await once(s)
        tot.update(c)
        by_mode[mode].update(c)
        strand += st
    print(
        f"{which} (py {sys.version.split()[0]}, gil={getattr(sys, '_is_gil_enabled', lambda: True)()}): "
        f"iterations={iters} jobs/iter={jobs} results={iters * jobs} "
        f"histogram(deliveries on head path -> results)={dict(sorted(tot.items()))} "
        f"by mode={ {m: dict(sorted(v.items())) for m, v in by_mode.items()} } "
        f"steers stranded in queue={strand} ({time.time() - t0:.1f}s)"
    )


asyncio.run(main())
