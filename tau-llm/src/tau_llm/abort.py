"""τ-llm abort: AbortSignal for async cancellation.

Reference: SUBPHASE-0.0.md, "3. AbortSignal" section.

AbortSignal is the Python equivalent of the Web API's AbortController: a
thread-safe flag an operation polls, plus who tripped it.

Usage:
    signal = AbortSignal()
    # In async operations:
    async def do_work():
        while not signal.is_aborted():
            await asyncio.sleep(0.1)
    # To abort:
    signal.abort()
"""

from __future__ import annotations

import threading
from typing import Literal

from tau_llm.docs import agent_facing

AbortReason = Literal["user", "extension"]
"""Who asked for an abort: a person (or a head acting for one), or an extension."""

ABORT_REASONS: tuple[AbortReason, ...] = ("user", "extension")


@agent_facing(topic="providers")
class AbortSignal:
    """Signal that can be checked during async operations.

    Once :meth:`abort` is called, every :meth:`is_aborted` returns True. Async
    operations check it periodically and stop. Thread-safe.

    Reference: SUBPHASE-0.0.md, "3. AbortSignal" section.
    """

    def __init__(self) -> None:
        self._aborted = False
        self._reason: AbortReason | None = None
        self._lock = threading.Lock()

    def is_aborted(self) -> bool:
        """Whether :meth:`abort` has been called.

        Returns:
            True if abort() has been called, False otherwise.
        """
        with self._lock:
            return self._aborted

    @property
    def reason(self) -> AbortReason | None:
        """Who aborted: ``"user"`` or ``"extension"``; ``None`` until aborted."""
        with self._lock:
            return self._reason

    def abort(self, reason: AbortReason = "user") -> None:
        """Signal that the current operation should be aborted.

        Idempotent. A ``"user"`` abort overrides an earlier ``"extension"`` one;
        the reverse changes nothing (docs/TURN-SETTINGS.md §4).

        Args:
            reason: ``"user"`` (the default, so an unreasoned caller means a
                person asked) or ``"extension"``.

        Raises:
            ValueError: ``reason`` is neither; the signal is left untripped.
        """
        if reason not in ABORT_REASONS:
            raise ValueError(f"abort reason must be 'user' or 'extension', got {reason!r}")
        with self._lock:
            if not self._aborted or reason == "user":
                self._reason = reason
            self._aborted = True
