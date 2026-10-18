"""τ-llm abort: AbortSignal for async cancellation.

Reference: SUBPHASE-0.0.md, "3. AbortSignal" section.

AbortSignal is the Python equivalent of the Web API's AbortController: a
thread-safe flag, plus who tripped it. An operation either polls
:meth:`AbortSignal.is_aborted` or awaits :meth:`AbortSignal.wait`; a read that
can block for an unbounded time goes through :func:`iterate_until_aborted` or
:func:`unless_aborted`, so an abort does not wait for the next chunk
(docs/TURN-SETTINGS.md §4).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterable, AsyncIterator, Awaitable
from typing import Any, Literal, TypeVar

from tau_llm.docs import agent_facing

AbortReason = Literal["user", "extension"]
"""Who asked for an abort: a person (or a head acting for one), or an extension."""

ABORT_REASONS: tuple[AbortReason, ...] = ("user", "extension")

_T = TypeVar("_T")


@agent_facing(topic="providers")
class AbortSignal:
    """Signal that can be checked during async operations.

    Once :meth:`abort` is called, every :meth:`is_aborted` returns True and every
    pending :meth:`wait` returns. Thread-safe.

    Reference: SUBPHASE-0.0.md, "3. AbortSignal" section.
    """

    def __init__(self) -> None:
        self._aborted = False
        self._reason: AbortReason | None = None
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

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
            waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            # a closed loop has no coroutine left awaiting this future
            if not loop.is_closed():
                loop.call_soon_threadsafe(_resolve, future)

    async def wait(self) -> None:
        """Return once :meth:`abort` has been called; at once if it already has.

        Safe to call from any event loop; :meth:`abort` may run on another thread.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._lock:
            if self._aborted:
                return
            self._waiters.append((loop, future))
        try:
            await future
        finally:
            with self._lock:
                if (loop, future) in self._waiters:
                    self._waiters.remove((loop, future))


def _resolve(future: asyncio.Future[None]) -> None:
    """Complete ``future`` unless its waiter was already cancelled."""
    if not future.done():
        future.set_result(None)


async def _cancel(task: asyncio.Future[Any]) -> None:
    """Cancel ``task`` and wait for it to finish, discarding its outcome."""
    task.cancel()
    # the read was abandoned on purpose, so how it ended is not a result
    await asyncio.gather(task, return_exceptions=True)


@agent_facing(topic="providers")
async def iterate_until_aborted(
    source: AsyncIterable[_T], signal: AbortSignal | None
) -> AsyncIterator[_T]:
    """Yield ``source``'s items, stopping as soon as ``signal`` trips.

    An abort cancels the pending read rather than waiting for the next item, so
    a silent or stalled stream stops at once. The iterator ends normally on an
    abort; the caller tells the two endings apart with ``signal.is_aborted()``.
    ``source`` is left cancelled mid-read and must not be iterated again.

    Args:
        source: The stream to read, e.g. ``response.aiter_lines()``.
        signal: The request's abort signal; ``None`` reads ``source`` unchanged.
    """
    if signal is None:
        async for item in source:
            yield item
        return
    iterator = aiter(source)
    aborted = asyncio.ensure_future(signal.wait())
    step: asyncio.Future[_T] | None = None
    try:
        while True:
            step = asyncio.ensure_future(anext(iterator))
            await asyncio.wait({step, aborted}, return_when=asyncio.FIRST_COMPLETED)
            if aborted.done():
                await _cancel(step)
                return
            try:
                item = step.result()
            except StopAsyncIteration:
                return
            step = None
            yield item
    finally:
        if step is not None and not step.done():
            await _cancel(step)
        await _cancel(aborted)


@agent_facing(topic="providers")
async def unless_aborted(awaitable: Awaitable[_T], signal: AbortSignal | None) -> _T | None:
    """Await ``awaitable`` unless ``signal`` trips first, cancelling it then.

    Args:
        awaitable: A single blocking call, e.g. a buffered HTTP request. It must
            not itself resolve to ``None``, because ``None`` here means aborted.
        signal: The request's abort signal; ``None`` awaits ``awaitable`` plainly.

    Returns:
        The awaitable's result, or ``None`` if ``signal`` tripped first.
    """
    if signal is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    aborted = asyncio.ensure_future(signal.wait())
    try:
        await asyncio.wait({task, aborted}, return_when=asyncio.FIRST_COMPLETED)
        if aborted.done():
            await _cancel(task)
            return None
        return task.result()
    finally:
        if not task.done():
            await _cancel(task)
        await _cancel(aborted)
