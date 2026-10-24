"""The unwind a suspended turn takes, and core's check that nothing swallowed it.

docs/STEP-DEBUGGER.md §3.2.2. Milestone 1 lands the type, the loop's mapping
and the runtime check; nothing raises :class:`TurnSuspended` until
``api.suspend`` (slice M1e) does, by calling :func:`mark_suspension_raised` and
then raising it.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from tau_llm.docs import agent_facing


@agent_facing(topic="agent-loop")
class TurnSuspended(BaseException):
    """Unwinds a turn parked at a suspension, out to the agent loop (§3.2.2).

    A ``BaseException``, so no ``except Exception`` catches it. Every catch
    that can see one lets it leave unchanged; the lint in
    ``tau-agent-core/tests/test_turn_suspended_lint.py`` enforces that over the
    five ``src`` trees. The loop maps it to ``agent_end`` with
    ``end_reason: "suspended"`` and writes nothing else.

    Args:
        args: The usual exception arguments.

    Attributes:
        messages: The turn's messages written before it unwound, filled in by
            the session on the way out; what ``submit_threadsafe`` reports.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.messages: list[dict[str, Any]] = []


@agent_facing(topic="agent-loop")
class SuspensionSwallowedError(BaseException):
    """A hook handler returned, or raised something else, after a suspension unwound in it.

    Raised by the runner in place of the handler's outcome, so the turn ends in
    error instead of running tools nobody stepped through (§3.2.2). A
    ``BaseException`` for the reason :class:`TurnSuspended` is one: the hook
    error convention, which reports an ``Exception`` and moves on to the next
    handler, must not absorb it.

    Attributes:
        extension: The extension whose handler the unwind did not leave.
        event: The hook being dispatched.
        raised_by: Who raised the suspension, as :func:`mark_suspension_raised`
            recorded it.
    """

    def __init__(self, extension: str, event: str, raised_by: str) -> None:
        super().__init__(
            f"extension {extension!r} swallowed a TurnSuspended raised by {raised_by!r} "
            f"during its {event!r} handler: the handler did not let it leave unchanged "
            "(docs/STEP-DEBUGGER.md §3.2.2). A catch that can see a BaseException must "
            "re-raise TurnSuspended."
        )
        self.extension = extension
        self.event = event
        self.raised_by = raised_by


@dataclass
class _DispatchMark:
    """One hook handler's dispatch: who, if anyone, raised a suspension inside it."""

    raised_by: str | None = None


_DISPATCH_MARK: contextvars.ContextVar[_DispatchMark | None] = contextvars.ContextVar(
    "tau_dispatch_mark", default=None
)


@contextmanager
def dispatch_mark() -> Iterator[_DispatchMark]:
    """Open a fresh mark for one hook handler's call; the runner wraps each handler in one.

    Yields:
        The mark, which the runner reads once the handler has finished.
    """
    mark = _DispatchMark()
    token = _DISPATCH_MARK.set(mark)
    try:
        yield mark
    finally:
        _DISPATCH_MARK.reset(token)


def mark_suspension_raised(raised_by: str) -> None:
    """Record, on the open dispatch, that a suspension is about to raise :class:`TurnSuspended`.

    The hook for ``api.suspend`` (M1e), which calls it just before raising.

    Args:
        raised_by: Who is suspending, named in the error if the unwind is swallowed.

    Raises:
        RuntimeError: no hook dispatch is open, so there is nothing to unwind to.
    """
    mark = _DISPATCH_MARK.get()
    if mark is None:
        raise RuntimeError("mark_suspension_raised: no hook dispatch is open")
    mark.raised_by = raised_by
