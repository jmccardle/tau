"""Bounded, visible retry of a request that failed before producing anything.

:func:`~tau_llm.client.stream_simple` wraps every provider stream in
:func:`retrying_stream`, so the agent loop, ``complete_simple`` and every
extension get the same policy. See docs/PROVIDER-RETRY.md.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from tau_llm.docs import agent_facing
from tau_llm.errors import ProviderConnectionError, ProviderHttpError
from tau_llm.streaming import DoneEvent, RetryEvent
from tau_llm.types import AssistantMessage, Usage

_logger = logging.getLogger(__name__)


RETRYABLE_STATUSES = frozenset({408, 429, 502, 503, 504, 529})
"""Statuses a retry can cure: overload, rate limit, a gateway, a model loading.

500 and 524 are left out. A 500 is as often a request the server cannot handle as
a passing fault, and a 524 is a gateway that already waited 100s for an answer
the server may still be computing.
"""


@agent_facing(topic="providers")
@dataclass(frozen=True)
class RetryPolicy:
    """How :func:`retrying_stream` retries. The default is :data:`DEFAULT_RETRY_POLICY`.

    Pass one as ``options["retry"]`` to ``stream_simple`` or ``complete_simple``;
    pass ``False`` to send exactly once.

    Attributes:
        max_retries: Re-sends after the first attempt; 0 sends once.
        base_delay: Seconds before the first retry; each later one doubles it.
        max_delay: Ceiling on one computed backoff before jitter, in seconds.
        max_retry_after: The longest ``Retry-After`` honoured, in seconds. A
            server that asks for longer gets no retry: its error is raised at once.
        jitter: Each computed backoff is stretched by up to this fraction, so
            concurrent callers that failed together do not retry together.
            A ``Retry-After`` is honoured exactly.
        statuses: The HTTP statuses retried.
    """

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    max_retry_after: float = 60.0
    jitter: float = 0.2
    statuses: frozenset[int] = field(default=RETRYABLE_STATUSES)

    def __post_init__(self) -> None:
        """Refuse a policy that cannot be followed as written."""
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        for name in ("base_delay", "max_delay", "max_retry_after", "jitter"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)}")

    def retryable(self, exc: BaseException) -> bool:
        """Whether this policy re-sends after ``exc``.

        Args:
            exc: What the attempt raised before producing any event.

        Returns:
            True for a :class:`ProviderHttpError` whose status is in ``statuses``,
            and for any :class:`ProviderConnectionError`.
        """
        if isinstance(exc, ProviderHttpError):
            return exc.status_code in self.statuses
        return isinstance(exc, ProviderConnectionError)

    def delay(self, retry: int, exc: BaseException, rng: Callable[[], float]) -> float | None:
        """Compute the wait before one retry.

        A ``Retry-After`` replaces the backoff and gets no jitter.

        Args:
            retry: Which retry this is, counting from 1.
            exc: The failure being retried; its ``retry_after`` is read when it
                is a :class:`ProviderHttpError`.
            rng: Returns a float in [0, 1) that scales the jitter.

        Returns:
            Seconds to wait: ``min(max_delay, base_delay * 2**(retry-1))``
            stretched by up to ``jitter``, or the server's ``Retry-After``. None
            when that ``Retry-After`` exceeds ``max_retry_after``: do not retry.
        """
        asked = exc.retry_after if isinstance(exc, ProviderHttpError) else None
        if asked is not None:
            return asked if asked <= self.max_retry_after else None
        backoff = min(self.max_delay, self.base_delay * 2.0 ** (retry - 1))
        return backoff * (1.0 + self.jitter * rng())


DEFAULT_RETRY_POLICY = RetryPolicy()
"""Three retries at about 1s, 2s and 4s, honouring ``Retry-After`` up to 60s."""

NO_RETRY = RetryPolicy(max_retries=0)
"""Send once. What ``options["retry"] = False`` resolves to."""


def resolve_retry_policy(value: Any) -> RetryPolicy:
    """The policy an ``options["retry"]`` value names.

    Absent (None) is the default, ``False`` is :data:`NO_RETRY`, and a
    :class:`RetryPolicy` is itself. Anything else raises ``TypeError``.
    """
    if value is None:
        return DEFAULT_RETRY_POLICY
    if value is False:
        return NO_RETRY
    if isinstance(value, RetryPolicy):
        return value
    raise TypeError(
        f"options['retry'] must be a tau_llm.retry.RetryPolicy or False, "
        f"got {type(value).__name__}: {value!r}"
    )


async def _wait_unless_aborted(seconds: float, abort_signal: Any) -> bool:
    """Sleep ``seconds``; True if ``abort_signal`` fired first."""
    if abort_signal is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(abort_signal.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _aborted_message(model: Any) -> AssistantMessage:
    """The empty ``stop_reason="aborted"`` message for an abort during a wait."""
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(),
        stop_reason="aborted",
        timestamp=int(time.time() * 1000),
    )


async def retrying_stream(
    first: AsyncIterator[Any],
    reopen: Callable[[], Awaitable[AsyncIterator[Any]]],
    policy: RetryPolicy,
    *,
    model: Any,
    abort_signal: Any = None,
    rng: Callable[[], float] = random.random,
) -> AsyncIterator[Any]:
    """Yield ``first``'s events, re-sending the request on a retryable early failure.

    A failure is retried only when the attempt yielded nothing, so a caller never
    sees output from two attempts. Each retry yields a :class:`RetryEvent` and
    logs a warning before its wait. An abort already set when the attempt fails,
    or set during the wait, ends the stream with an empty ``"aborted"`` message
    and no retry announced, as an abort before any output does.

    Args:
        first: The first attempt's provider stream.
        reopen: Builds the next attempt from the same arguments as ``first``,
            so the request is byte-identical and a prompt cache still matches.
        policy: Which failures to retry, how often and how long to wait.
        model: The model, for the aborted message.
        abort_signal: The request's ``AbortSignal``, checked during each wait.
        rng: Returns a float in [0, 1) for jitter; injectable for tests.
    """
    stream = first
    retries = 0
    while True:
        produced = False
        try:
            async for event in stream:
                produced = True
                yield event
            return
        except Exception as exc:
            if produced or retries >= policy.max_retries or not policy.retryable(exc):
                raise
            wait = policy.delay(retries + 1, exc, rng)
            if wait is None:
                _logger.warning(
                    "not retrying: the server asked to wait longer than %.0fs: %s",
                    policy.max_retry_after,
                    exc,
                )
                raise
            failure = exc
        if abort_signal is not None and abort_signal.is_aborted():
            message = _aborted_message(model)
            yield DoneEvent(final=message, usage=message.usage)
            return
        retries += 1
        _logger.warning("retry %d of %d in %.1fs: %s", retries, policy.max_retries, wait, failure)
        yield RetryEvent(
            attempt=retries, max_retries=policy.max_retries, delay_seconds=wait, error=failure
        )
        if await _wait_unless_aborted(wait, abort_signal):
            message = _aborted_message(model)
            yield DoneEvent(final=message, usage=message.usage)
            return
        stream = await reopen()
