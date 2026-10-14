"""Hammer the check-then-add warn-once idiom (providers/anthropic.py:262) with no artificial delay."""
import sys
import threading

TRIALS, T = 3000, 16
dups = 0
for _ in range(TRIALS):
    warned: set[str] = set()
    fired = []
    b = threading.Barrier(T)

    def w():
        b.wait()
        if "m" not in warned:
            warned.add("m")
            fired.append(1)

    ts = [threading.Thread(target=w) for _ in range(T)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dups += len(fired) > 1
print(f"gil={sys._is_gil_enabled()} trials with >1 'first' warning: {dups}/{TRIALS}")
