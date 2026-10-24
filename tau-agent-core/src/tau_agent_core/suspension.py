"""The dispatch mark ``api.suspend`` reads, the unwind a suspended turn takes, and its check.

docs/STEP-DEBUGGER.md §3.2 and §3.2.2. In milestone 1 a suspension waits for a
live responder and never raises :class:`TurnSuspended`; the deferred responder
(milestone 2) will, by calling :func:`mark_suspension_raised` and then raising.
The suspensions themselves live in :mod:`tau_agent_core.suspensions`.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from tau_llm.docs import agent_facing

from tau_agent_core.policies import runs_on_behalf_of


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
    """One hook handler's dispatch: where it runs, and who, if anyone, raised a suspension in it.

    It is also the dispatch-scoped permission ``api.suspend`` reads (§3.2): a
    context variable holds it for exactly one handler call. ``closed`` is set
    once the handler's dispatch returns, so a task the handler spawned, whose
    context still holds the mark, can tell it has nothing to unwind to.

    Attributes:
        raised_by: Who raised a suspension inside the dispatch, or ``None``.
        closed: Whether the dispatch has returned.
        hook: The hook being dispatched, or ``None`` for a bare mark.
        extension: The extension whose handler is running.
        handler: The handler's position in the dispatch, in load and
            registration order, from 0.
        event: The event the handler was given.
        task: The task the dispatch runs on; another task acts for it only
            through :func:`~tau_agent_core.policies.runs_on_behalf_of`.
    """

    raised_by: str | None = None
    closed: bool = False
    hook: str | None = None
    extension: str | None = None
    handler: int = 0
    event: dict[str, Any] | None = None
    task: asyncio.Task[Any] | None = None


_DISPATCH_MARK: contextvars.ContextVar[_DispatchMark | None] = contextvars.ContextVar(
    "tau_dispatch_mark", default=None
)


def current_dispatch_mark() -> _DispatchMark | None:
    """The mark of the hook dispatch this context runs in, closed or not; ``None`` outside one."""
    return _DISPATCH_MARK.get()


@contextmanager
def dispatch_mark(
    hook: str | None = None,
    extension: str | None = None,
    event: dict[str, Any] | None = None,
    handler: int = 0,
) -> Iterator[_DispatchMark]:
    """Open a fresh mark for one hook handler's call; the runner wraps each handler in one.

    Args:
        hook: The hook being dispatched.
        extension: The handler's extension.
        event: The event the handler is given.
        handler: The handler's position in the dispatch.

    Yields:
        The mark, which the runner reads once the handler has finished.
    """
    mark = _DispatchMark(
        hook=hook,
        extension=extension,
        handler=handler,
        event=event,
        task=asyncio.current_task(),
    )
    token = _DISPATCH_MARK.set(mark)
    try:
        yield mark
    finally:
        mark.closed = True
        _DISPATCH_MARK.reset(token)


@contextmanager
def outside_dispatch() -> Iterator[None]:
    """Run the body with no dispatch mark: a notify handler inherits no suspension permission (§3.2)."""
    token = _DISPATCH_MARK.set(None)
    try:
        yield
    finally:
        _DISPATCH_MARK.reset(token)


def dispatch_refusal(mark: _DispatchMark | None) -> str | None:
    """Why the code running now cannot act for ``mark``'s dispatch, or ``None`` when it can.

    Args:
        mark: The mark this context holds.

    Returns:
        No dispatch, one that has returned, or a task that is not the
        dispatch's own (a task the handler spawned); a policed handler's task
        acts for its dispatch.
    """
    if mark is None:
        return "no hook dispatch is open here (a notify event, a tool, a command, or a task)"
    if mark.closed:
        return f"the {mark.hook!r} dispatch this context came from has returned"
    if mark.task is not None and not runs_on_behalf_of(mark.task):
        return (
            f"this runs on a task the {mark.hook!r} handler spawned, not on the dispatch's own task"
        )
    return None


def mark_suspension_raised(raised_by: str) -> None:
    """Record, on the open dispatch, that a suspension is about to raise :class:`TurnSuspended`.

    The deferred path of ``api.suspend`` (milestone 2) calls it once the spec is
    validated, just before raising.

    Args:
        raised_by: Who is suspending, named in the error if the unwind is swallowed.

    Raises:
        RuntimeError: no hook dispatch is open, the one this context holds has
            returned, or this is a task other than the dispatch's own, so there
            is nothing to unwind to.
    """
    mark = _DISPATCH_MARK.get()
    refusal = dispatch_refusal(mark)
    if refusal is not None or mark is None:
        raise RuntimeError(f"mark_suspension_raised: {refusal}")
    mark.raised_by = raised_by
