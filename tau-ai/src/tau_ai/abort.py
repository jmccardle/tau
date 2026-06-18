"""τ-ai abort: AbortSignal for async cancellation.

Reference: SUBPHASE-0.0.md, "3. AbortSignal" section.

AbortSignal is the Python equivalent of the Web API's AbortController.
It provides thread-safe abort signaling for async operations.

Usage:
    signal = AbortSignal()
    # In async operations:
    async def do_work():
        while not signal.is_aborted():
            # Do work
            await asyncio.sleep(0.1)
    # To abort:
    signal.abort()
"""

from __future__ import annotations

import threading


class AbortSignal:
    """Signal that can be checked during async operations.

    When abort() is called, all subsequent is_aborted() checks return True.
    Async operations should check this periodically (e.g., every 100ms)
    and raise asyncio.CancelledError if aborted.

    Reference: SUBPHASE-0.0.md, "3. AbortSignal" section.

    Attributes:
        _aborted: Internal state (private)
        _lock: Threading lock for thread safety (private)
    """

    def __init__(self) -> None:
        self._aborted = False
        self._lock = threading.Lock()

    def is_aborted(self) -> bool:
        """Check if the signal has been aborted.

        Thread-safe: uses threading.Lock internally.
        Idempotent: multiple calls return the same value.

        Returns:
            True if abort() has been called, False otherwise.
        """
        with self._lock:
            return self._aborted

    def abort(self) -> None:
        """Signal that the current operation should be aborted.

        Idempotent: calling multiple times has no additional effect.
        Thread-safe: uses threading.Lock internally.
        """
        with self._lock:
            self._aborted = True
