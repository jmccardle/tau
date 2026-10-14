"""What loop confinement buys: the registry's handle counter hammered from threads.

Not how τ calls it (every registry method runs on the session's loop and
_check_loop refuses any other); this measures what would happen if a sync tool
or a foreign thread reached the registry directly.
"""
import sys
import threading

from tau_agent_core.extensions_builtin import background as bg

THREADS, PER = 8, 50_000


def run() -> tuple[int, int]:
    reg = bg.BackgroundRegistry(session_id=lambda: "s")
    out: list[list[str]] = [[] for _ in range(THREADS)]
    barrier = threading.Barrier(THREADS)

    def work(i: int) -> None:
        barrier.wait()
        mine = out[i]
        for _ in range(PER):
            mine.append(reg._next_handle("t"))

    ts = [threading.Thread(target=work, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    handles = [h for part in out for h in part]
    return len(handles) - len(set(handles)), reg._counter


gil = getattr(sys, "_is_gil_enabled", lambda: True)()
for trial in range(5):
    dupes, counter = run()
    print(
        f"py {sys.version.split()[0]} gil={gil} trial {trial}: {THREADS}x{PER} handles minted, "
        f"duplicates={dupes}, final counter={counter} (expected {THREADS * PER})"
    )
