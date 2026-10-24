"""Handler policies: how a hook or ``ext:`` handler is run, and what its failure becomes.

docs/STEP-DEBUGGER.md §4 (#83). **Experimental:** below 1.0 the names, the
outcome types and the built-in numbers may change.

A policy is anything with ``async run(call, site) -> Outcome``. Core runs a
handler registered with ``api.on(event, handler, policy="name")`` through it;
the hook site then interprets the :data:`Outcome`. A handler with no policy is
run exactly as before policies existed.

Deadlines are pausable so that a suspension (``api.suspend``, slice M1e) can
stop every enclosing clock while a human reads: :func:`pause_enclosing_deadlines`.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from tau_llm.docs import agent_facing

FailureMode = Literal["fail", "proceed"]
"""What a :class:`Bounded` or :class:`Unbounded` policy makes of a failure."""

_FAILURE_MODES: tuple[str, ...] = ("fail", "proceed")


@agent_facing(topic="extensions", since="0.15.0")
@dataclass(frozen=True)
class Returned:
    """The handler finished; the site uses ``value`` as the handler's return.

    Attributes:
        value: What the handler returned.
    """

    value: Any


@agent_facing(topic="extensions", since="0.15.0")
@dataclass(frozen=True)
class Failed:
    """The handler failed, and the failure means what failure means at the site.

    ``tool_call`` blocks the call; ``annotate`` records the error and aborts the
    turn; every other site raises :class:`HandlerPolicyError`, which ends the turn
    (or, on an ``ext:`` channel, raises into the emitter's ``api.emit``).

    Attributes:
        error: Why: the handler's exception, or :class:`DeadlineExpired`.
    """

    error: Exception

    def __post_init__(self) -> None:
        """Refuse an error that is not an ``Exception``; a ``BaseException`` must propagate."""
        if not isinstance(self.error, Exception):
            raise TypeError(f"Failed.error must be an Exception, not {type(self.error).__name__}")


@agent_facing(topic="extensions", since="0.15.0")
@dataclass(frozen=True)
class Proceeded:
    """The handler failed, and work continues as if it had returned nothing.

    ``record`` is kept where the site has a place for it: at ``annotate`` as
    ``ext[<extension>] = {"error": record, "proceeded": true}``, elsewhere as a
    session warning keyed ``policy:<hook>``.

    Attributes:
        record: One line saying what failed.
    """

    record: str

    def __post_init__(self) -> None:
        """Refuse an empty or non-string record; a proceeded failure is never silent."""
        if not isinstance(self.record, str) or not self.record.strip():
            raise ValueError("Proceeded.record must be a non-empty string")


Outcome: TypeAlias = Returned | Failed | Proceeded
"""What a :class:`HandlerPolicy` returns for one handler call."""


@agent_facing(topic="extensions", since="0.15.0")
class HandlerPolicyError(Exception):
    """A policed handler's ``Failed`` outcome, raised where the site does not isolate it.

    Raised out of ``turn_end``, ``tool_result``, ``entry_written`` and the other
    isolating hooks, which ends the turn with it, and into the emitter's
    ``await api.emit(...)`` for an ``ext:`` channel handler.

    Attributes:
        extension: The extension whose handler failed.
        event: The hook or ``ext:`` channel being dispatched.
        policy: The policy's registered name.
        detail: ``"<ExceptionType>: <message>"`` of the underlying failure.
    """

    def __init__(self, extension: str, event: str, policy: str, error: Exception) -> None:
        """Name the handler and wrap ``error``, which also becomes ``__cause__``."""
        self.extension = extension
        self.event = event
        self.policy = policy
        self.detail = describe(error)
        super().__init__(
            f"extension {extension!r} {event!r} handler failed under policy {policy!r}: "
            f"{self.detail}"
        )
        self.__cause__ = error


@agent_facing(topic="extensions", since="0.15.0")
class DeadlineExpired(TimeoutError):
    """:meth:`PausableDeadline.run`'s call ran past its deadline and was cancelled.

    Attributes:
        deadline: The deadline that expired.
    """

    def __init__(self, deadline: PausableDeadline) -> None:
        """Name the expired deadline."""
        super().__init__(f"timed out after {deadline.seconds:g}s")
        self.deadline = deadline


def describe(error: BaseException) -> str:
    """``"<ExceptionType>: <message>"``, or the bare type name when the message is empty.

    Args:
        error: Any exception.

    Returns:
        The one-line description an outcome record or an ``ext`` error uses.
    """
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _check_seconds(field: str, seconds: Any) -> None:
    """Refuse a budget that is not a positive, finite number."""
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError(f"{field} must be a positive, finite number of seconds, not {seconds!r}")


_ENCLOSING: contextvars.ContextVar[tuple[PausableDeadline, ...]] = contextvars.ContextVar(
    "tau_enclosing_deadlines", default=()
)


_AWAITED_BY: weakref.WeakKeyDictionary[asyncio.Task[Any], asyncio.Task[Any]] = (
    weakref.WeakKeyDictionary()
)


@agent_facing(topic="extensions", since="0.15.0")
def runs_on_behalf_of(task: asyncio.Task[Any]) -> bool:
    """Whether the current task is ``task``, or a policed task that ``task`` is awaiting.

    A bounded handler runs in its own task, so a guard that compares
    ``asyncio.current_task()`` with a turn's task would miss it. This follows
    each :meth:`PausableDeadline.run` task to the task awaiting it. A task a
    handler merely spawned is not followed, so it is never mistaken for the
    turn.

    Args:
        task: The task to test against, e.g. the one driving a turn.

    Returns:
        True when the current code runs as part of ``task``.
    """
    current = asyncio.current_task()
    while current is not None:
        if current is task:
            return True
        current = _AWAITED_BY.get(current)
    return False


@agent_facing(topic="extensions", since="0.15.0")
@contextmanager
def acting_for(awaiter: asyncio.Task[Any] | None) -> Iterator[None]:
    """Count the current task as ``awaiter``'s for :func:`runs_on_behalf_of` while the body runs.

    For a child task its awaiter waits on, such as a parallel tool batch's
    ``gather`` children, so a hook running in one is still seen as the turn's
    (#96). ``None`` links nothing.

    Args:
        awaiter: The task awaiting the current one.
    """
    current = asyncio.current_task()
    if awaiter is None or current is None or current is awaiter:
        yield
        return
    _AWAITED_BY[current] = awaiter
    try:
        yield
    finally:
        _AWAITED_BY.pop(current, None)


@agent_facing(topic="extensions", since="0.15.0")
def enclosing_deadlines() -> tuple[PausableDeadline, ...]:
    """Every policy deadline the current code runs under, outermost first.

    A policed handler's task carries its own deadline and every deadline of the
    dispatches it is nested in, through ``contextvars``.

    Returns:
        The deadlines; empty outside any bounded handler.
    """
    return _ENCLOSING.get()


@agent_facing(topic="extensions", since="0.15.0")
@contextmanager
def pause_enclosing_deadlines() -> Iterator[tuple[PausableDeadline, ...]]:
    """Pause every enclosing deadline for the body, then resume them (§4.3).

    ``api.suspend`` (M1e) wraps its whole wait in this, queue time included, so
    a timeout measures a handler's own work and never a human's reading time.

    Yields:
        The deadlines paused, outermost first.
    """
    paused = enclosing_deadlines()
    for deadline in paused:
        deadline.pause()
    try:
        yield paused
    finally:
        for deadline in reversed(paused):
            deadline.resume()


@agent_facing(topic="extensions", since="0.15.0")
class PausableDeadline:
    """A time budget for one call, run in its own task and cancelled when the budget is spent.

    :meth:`run` starts the clock; :meth:`pause` and :meth:`resume` stop and
    restart it, and nest. A deadline resumed with no time left fires at once.
    An enclosing deadline caps this one: when it expires, its cancellation
    reaches this deadline's task, and the enclosing policy's timeout applies.

    Args:
        seconds: The budget; positive and finite.

    Attributes:
        seconds: The budget it was created with.
        expired: Whether it fired and cancelled its call.
    """

    def __init__(self, seconds: float) -> None:
        _check_seconds("a deadline's seconds", seconds)
        self.seconds = float(seconds)
        self.expired = False
        self._loop = asyncio.get_running_loop()
        self._enclosing = enclosing_deadlines()
        self._left = self.seconds
        self._due: float | None = None
        self._pauses = 0
        self._task: asyncio.Task[Any] | None = None
        self._timer: asyncio.TimerHandle | None = None

    @property
    def paused(self) -> bool:
        """Whether at least one :meth:`pause` is outstanding."""
        return self._pauses > 0

    def _own_left(self) -> float:
        """This deadline's own time left, ignoring the enclosing ones."""
        if self._due is None:
            return self._left
        return self._due - self._loop.time()

    def remaining(self) -> float:
        """Seconds left before this call is cancelled, capped by every enclosing deadline.

        Returns:
            The smallest time left among this deadline and its enclosing ones;
            zero or less once spent.
        """
        return min([self._own_left(), *(outer.remaining() for outer in self._enclosing)])

    async def run(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``call`` in its own task under this deadline and return its value.

        The task copies the current context, with this deadline appended to
        the :func:`enclosing_deadlines` of the code that calls this. Any exception, ``BaseException`` included,
        leaves unchanged; only this deadline's own cancellation becomes
        :class:`DeadlineExpired`.

        Args:
            call: Called once, in the new task.

        Returns:
            What ``call`` returned.

        Raises:
            DeadlineExpired: the deadline fired first.
            RuntimeError: this deadline already ran a call.
        """
        if self._task is not None:
            raise RuntimeError("a PausableDeadline runs one call")
        self._enclosing = enclosing_deadlines()
        context = contextvars.copy_context()
        context.run(_ENCLOSING.set, (*self._enclosing, self))

        async def _body() -> Any:
            return await call()

        task = self._loop.create_task(_body(), context=context)
        self._task = task
        awaiter = asyncio.current_task()
        if awaiter is not None:
            _AWAITED_BY[task] = awaiter
        if not self.paused:
            self._start_clock()
        try:
            value = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.expired and (current is None or current.cancelling() == 0):
                raise DeadlineExpired(self) from None
            raise
        finally:
            self._stop_clock()
            _AWAITED_BY.pop(task, None)
        if self.expired:
            raise DeadlineExpired(self)
        return value

    def pause(self) -> None:
        """Stop the clock; nested pauses need as many :meth:`resume` calls."""
        self._pauses += 1
        if self._pauses == 1 and self._due is not None:
            self._left = self._due - self._loop.time()
            self._stop_clock()

    def resume(self) -> None:
        """Undo one :meth:`pause`; the last one restarts the clock, firing at once if none is left.

        Raises:
            RuntimeError: there is no outstanding pause.
        """
        if self._pauses == 0:
            raise RuntimeError("PausableDeadline.resume without a matching pause")
        self._pauses -= 1
        if self._pauses == 0 and self._task is not None and not self._task.done():
            self._start_clock()

    def _start_clock(self) -> None:
        """Arm the watchdog for the time left, or fire now when there is none."""
        if self._left <= 0:
            self._fire()
            return
        self._due = self._loop.time() + self._left
        self._timer = self._loop.call_at(self._due, self._fire)

    def _stop_clock(self) -> None:
        """Disarm the watchdog and freeze the time left."""
        if self._due is not None:
            self._left = self._due - self._loop.time()
            self._due = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        """The watchdog: cancel the call's task, if it is still running."""
        self._timer = None
        self._due = None
        self._left = 0.0
        if self._task is not None and not self._task.done() and self._task.cancel():
            self.expired = True


@agent_facing(topic="extensions", since="0.15.0")
class PolicySite:
    """Where a policed handler is being run, handed to :meth:`HandlerPolicy.run`.

    Attributes:
        hook: The hook name, or the ``ext:`` channel.
        extension: The extension whose handler this is.
    """

    def __init__(self, hook: str, extension: str, warn: Callable[[str], None]) -> None:
        """Bind the site to its hook, extension and warning sink.

        Args:
            hook: The hook name, or the ``ext:`` channel.
            extension: The handler's extension.
            warn: Sets this handler's session warning; one line.
        """
        self.hook = hook
        self.extension = extension
        self._warn = warn

    def deadline(self, seconds: float) -> PausableDeadline:
        """A new deadline under every enclosing one.

        Args:
            seconds: The budget; positive and finite.

        Returns:
            The deadline, whose :meth:`PausableDeadline.run` runs the call.
        """
        return PausableDeadline(seconds)

    def warn(self, text: str) -> None:
        """Set the session warning ``policy:<hook>`` from this extension, replacing any earlier one.

        Args:
            text: What to say; whitespace, newlines included, collapses to one line.
        """
        self._warn(" ".join(text.split()) or "(no detail)")


@agent_facing(topic="extensions", since="0.15.0")
@runtime_checkable
class HandlerPolicy(Protocol):
    """How a handler is run and what its failure becomes. Experimental (§4.1).

    A policy that retries, falls back to another decider, or proceeds and
    records is a new class; core needs no change. ``run`` must let a
    ``BaseException`` that is not ``asyncio.CancelledError`` from its own
    deadline leave unchanged: a suspension unwinds through it.
    """

    async def run(self, call: Callable[[], Awaitable[Any]], site: PolicySite) -> Outcome:
        """Run ``call`` (the handler, bound to its event) once or more, and say how it went.

        Args:
            call: Calls the handler and awaits it.
            site: Where it runs; the source of deadlines and warnings.

        Returns:
            The outcome the site interprets.
        """
        ...


def _failure(mode: FailureMode, error: Exception) -> Outcome:
    """The outcome ``mode`` makes of ``error``."""
    return Failed(error) if mode == "fail" else Proceeded(describe(error))


def _check_mode(field: str, mode: str) -> None:
    """Refuse a failure mode that is not ``"fail"`` or ``"proceed"``."""
    if mode not in _FAILURE_MODES:
        raise ValueError(f"{field} must be one of {list(_FAILURE_MODES)}, not {mode!r}")


@agent_facing(topic="extensions", since="0.15.0")
@dataclass(frozen=True)
class Bounded:
    """Run the handler under a :class:`PausableDeadline`; a timeout or an error becomes ``on_*``.

    Attributes:
        timeout_s: The deadline in seconds.
        on_timeout: ``"fail"`` gives :class:`Failed`, ``"proceed"`` :class:`Proceeded`.
        on_error: The same, for an exception the handler raised.
    """

    timeout_s: float
    on_timeout: FailureMode
    on_error: FailureMode

    def __post_init__(self) -> None:
        """Validate the timeout and the modes now, not at the first call."""
        _check_seconds("timeout_s", self.timeout_s)
        _check_mode("on_timeout", self.on_timeout)
        _check_mode("on_error", self.on_error)

    async def run(self, call: Callable[[], Awaitable[Any]], site: PolicySite) -> Outcome:
        """Run ``call`` under ``site.deadline(timeout_s)``.

        Args:
            call: The bound handler.
            site: Where it runs.

        Returns:
            :class:`Returned`, or ``on_timeout``'s or ``on_error``'s outcome.
        """
        deadline = site.deadline(self.timeout_s)
        try:
            value = await deadline.run(call)
        except DeadlineExpired as err:
            mode = self.on_timeout if err.deadline is deadline else self.on_error
            return _failure(mode, err)
        except Exception as err:
            return _failure(self.on_error, err)
        return Returned(value)


@agent_facing(topic="extensions", since="0.15.0")
@dataclass(frozen=True)
class Unbounded:
    """Run the handler with no deadline, in the dispatch's own task; an error becomes ``on_error``.

    Attributes:
        on_error: ``"fail"`` gives :class:`Failed`, ``"proceed"`` :class:`Proceeded`.
    """

    on_error: FailureMode = "fail"

    def __post_init__(self) -> None:
        """Validate the mode now, not at the first failure."""
        _check_mode("on_error", self.on_error)

    async def run(self, call: Callable[[], Awaitable[Any]], site: PolicySite) -> Outcome:
        """Await ``call``.

        Args:
            call: The bound handler.
            site: Where it runs; unused.

        Returns:
            :class:`Returned`, or ``on_error``'s outcome.
        """
        try:
            value = await call()
        except Exception as err:
            return _failure(self.on_error, err)
        return Returned(value)


BUILTIN_POLICIES: dict[str, HandlerPolicy] = {
    "gating": Bounded(timeout_s=30, on_timeout="fail", on_error="fail"),
    "advisory": Bounded(timeout_s=15, on_timeout="proceed", on_error="proceed"),
    "human": Unbounded(on_error="fail"),
}
"""The policies every session starts with (§4.2; John's numbers, 2026-10-24)."""


def check_policy(policy: Any) -> HandlerPolicy:
    """``policy`` itself, once it is shown to have a callable ``run``.

    Args:
        policy: The object to register.

    Returns:
        ``policy``, typed.

    Raises:
        TypeError: it has no callable ``run``.
    """
    if not callable(getattr(policy, "run", None)):
        raise TypeError(
            f"a handler policy needs an async run(call, site) method; {type(policy).__name__} has none"
        )
    checked: HandlerPolicy = policy
    return checked


@dataclass(frozen=True)
class PolicedHandler:
    """A hook handler registered with a policy; the runner runs ``handler`` through ``policy``.

    Not callable, so nothing can call the handler around its policy by mistake.

    Attributes:
        handler: The extension's handler, ``(event, ctx)``.
        policy: The resolved policy.
        name: The name it was registered under.
    """

    handler: Callable[..., Any]
    policy: HandlerPolicy
    name: str


def check_outcome(outcome: Any, policy: str) -> Outcome:
    """``outcome`` itself, once it is shown to be one of the three outcome types.

    Args:
        outcome: What a policy's ``run`` returned.
        policy: The policy's name, for the error.

    Returns:
        ``outcome``, typed.

    Raises:
        TypeError: it is something else.
    """
    if not isinstance(outcome, (Returned, Failed, Proceeded)):
        raise TypeError(
            f"policy {policy!r} returned {type(outcome).__name__}; run must return "
            "Returned, Failed or Proceeded"
        )
    return outcome
