"""N agent sessions, each on its own event loop in its own OS thread, in ONE process.

Each session runs M prompts against the fake OpenAI server; each prompt does a
4-way parallel tool batch (find/grep/read/ls) then a text turn. Verifies every
final answer and reports wall time. This is the embedding shape free-threading
would actually speed up.
"""
import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import InMemorySessionLog

PORT, N, M, CWD = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
EXPECT = "FAKE-DONE saw 4 tool results (4 non-empty)."


async def one_session(k):
    s = create_agent_session(
        model="fake", provider="openai", base_url=f"http://127.0.0.1:{PORT}/v1",
        api_key="x", tools=["read", "grep", "find", "ls"], session_log=InMemorySessionLog(),
        no_context_files=True, cwd=CWD,
    )
    bad = []
    for i in range(M):
        msgs = await s.prompt(f"go {k}/{i}")
        texts = [b.get("text", "") for m in msgs if m.get("role") == "assistant"
                 for b in (m.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
        final = "".join(texts).strip()
        if final != EXPECT:
            bad.append(final[:120])
    return bad


def thread_main(k):
    return asyncio.run(one_session(k))


print("python", sys.version.split()[0], "gil", sys._is_gil_enabled())
t0 = time.perf_counter()
thread_main(-1)
one = time.perf_counter() - t0
t0 = time.perf_counter()
with ThreadPoolExecutor(N) as ex:
    results = list(ex.map(thread_main, range(N)))
wall = time.perf_counter() - t0
bad = [b for r in results for b in r]
print(f"sessions={N} prompts/session={M} bad={len(bad)} one_session={one:.2f}s "
      f"N_threads_wall={wall:.2f}s serial_equiv={N*one:.2f}s speedup={N*one/wall:.2f}x "
      f"threads_alive={threading.active_count()}")
for b in bad[:5]:
    print("  BAD:", b)
