"""A thread-safe "first time for this key" set, for warn-once diagnostics.

``if k not in s: s.add(k); warn()`` is a check-then-act race once threads run
without the GIL (experiments/freethreading/RESULTS.md §5 measured duplicate
warnings on 3.14t); :meth:`OnceSet.first` makes the check and the add one step.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable


class OnceSet:
    """Remembers keys; :meth:`first` is True exactly once per key, across threads."""

    def __init__(self) -> None:
        self._seen: set[Hashable] = set()
        self._lock = threading.Lock()

    def first(self, key: Hashable) -> bool:
        """Record ``key`` and report whether this call was the first to do so.

        Args:
            key: The thing being warned about, e.g. a model id.

        Returns:
            True for exactly one caller per key; False for every later one.
        """
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def clear(self) -> None:
        """Forget every key, so each warns again (test isolation)."""
        with self._lock:
            self._seen.clear()

    def __contains__(self, key: object) -> bool:
        """Whether ``key`` has been recorded."""
        with self._lock:
            return key in self._seen

    def __len__(self) -> int:
        """How many keys have been recorded."""
        with self._lock:
            return len(self._seen)
