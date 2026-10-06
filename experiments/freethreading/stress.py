"""Thread-stress τ's shared structures under free-threading.

1. tau_llm.tokens.character_classes: module-level _CLASS_OF cache filled lazily.
2. tau_llm.abort.AbortSignal: locked flag polled from worker threads.
3. Warn-once sets (check-then-add): count duplicate "first" warnings.
4. N independent agent sessions, each on its own event loop in its own thread,
   driving the find/grep/read tools concurrently (the multi-session embedding case).
"""
import asyncio
import sys
import threading
import time
import random
import string
from concurrent.futures import ThreadPoolExecutor

from tau_llm import tokens
from tau_llm.abort import AbortSignal

print("python", sys.version.split()[0], "gil", sys._is_gil_enabled())

# 1. shared lazy cache
random.seed(1)
alphabet = string.printable + "".join(chr(c) for c in range(0x400, 0x4ff))
texts = ["".join(random.choice(alphabet) for _ in range(20000)) for _ in range(64)]
expected = [tokens.character_classes(t) for t in texts]
tokens._CLASS_OF.clear()
barrier = threading.Barrier(16)


def work(i):
    barrier.wait()
    bad = 0
    for _ in range(20):
        for j, t in enumerate(texts):
            if tokens.character_classes(t) != expected[j]:
                bad += 1
    return bad


t0 = time.perf_counter()
with ThreadPoolExecutor(16) as ex:
    bad = sum(ex.map(work, range(16)))
print(f"[1] character_classes x16 threads: mismatches={bad} ({time.perf_counter()-t0:.2f}s)")

# 2. AbortSignal
sig = AbortSignal()
seen_after = []
stop = threading.Event()


def poller():
    n = 0
    while not sig.is_aborted():
        n += 1
    seen_after.append(n)


ths = [threading.Thread(target=poller) for _ in range(16)]
for th in ths:
    th.start()
time.sleep(0.2)
sig.abort()
for th in ths:
    th.join(5)
print(f"[2] AbortSignal: {len(seen_after)}/16 pollers observed abort; alive={sum(th.is_alive() for th in ths)}")

# 3. warn-once check-then-add (pattern copied from providers/anthropic.py:262)
dups = 0
for trial in range(200):
    warned = set()
    fired = []
    b = threading.Barrier(8)

    def w():
        b.wait()
        if "m" not in warned:
            warned.add("m")
            fired.append(1)

    ts = [threading.Thread(target=w) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    dups += len(fired) > 1
print(f"[3] warn-once pattern: trials with duplicate warning = {dups}/200")

# 4. per-thread sessions using real tools (no model): call tool.execute directly
from tau_agent_core.tools import create_coding_tools  # noqa: E402

ROOT = sys.argv[1]


async def drive(k):
    tools = {t.name: t for t in create_coding_tools(ROOT)}
    outs = []
    for i in range(10):
        outs.append(await tools["find"].execute(f"f{k}{i}", {"name": "*.py", "path": "tau-llm"}))
        outs.append(await tools["grep"].execute(f"g{k}{i}", {"pattern": "class ", "path": "tau-llm/src"}))
        outs.append(await tools["read"].execute(f"r{k}{i}", {"path": "tau-llm/src/tau_llm/abort.py"}))
    return [o["content"][0]["text"] for o in outs]


def run_thread(k):
    return asyncio.run(drive(k))


t0 = time.perf_counter()
ref = run_thread(0)
single = time.perf_counter() - t0
t0 = time.perf_counter()
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(run_thread, range(8)))
multi = time.perf_counter() - t0
diff = sum(r != ref for r in res)
print(f"[4] 8 threads x own loop x 30 tool calls: divergent={diff}/8 single={single:.2f}s 8x={multi:.2f}s speedup={8*single/multi:.2f}x")
