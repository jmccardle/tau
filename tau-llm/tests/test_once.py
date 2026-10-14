"""``OnceSet``: the warn-once primitive the providers share.

The bare ``if k not in s: s.add(k)`` it replaces fired twice for one key in 8 of
3000 trials on 3.14t (experiments/freethreading/RESULTS.md §5).
"""

from __future__ import annotations

import threading

from tau_llm.once import OnceSet


def test_first_is_true_once_per_key() -> None:
    seen = OnceSet()
    assert seen.first("m")
    assert not seen.first("m")
    assert seen.first("n")
    assert "m" in seen and len(seen) == 2
    seen.clear()
    assert not seen and seen.first("m")


def test_first_is_true_for_exactly_one_racing_thread() -> None:
    for _ in range(300):
        seen = OnceSet()
        barrier = threading.Barrier(16)
        wins: list[bool] = []

        def race() -> None:
            barrier.wait()
            wins.append(seen.first("model"))

        threads = [threading.Thread(target=race) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert wins.count(True) == 1
