"""A provider retry as an agent event, and the one sentence every head shows for it.

See docs/PROVIDER-RETRY.md §5. :func:`retry_notice` is pure, so the TUI, the REPL,
``tau -p`` and an RPC host that imports it word a retry the same way.
"""

from __future__ import annotations

import time
from typing import Any

from tau_llm.docs import agent_facing
from tau_llm.errors import ProviderHttpError
from tau_llm.streaming import RetryEvent

from tau_agent_core.events import AgentEvent


def provider_retry_event(event: RetryEvent) -> AgentEvent:
    """The ``provider_retry`` agent event for one :class:`~tau_llm.streaming.RetryEvent`."""
    error = event.error
    return AgentEvent(
        type="provider_retry",
        timestamp=int(time.time() * 1000),
        error=str(error) or type(error).__name__,
        attempt=event.attempt,
        max_retries=event.max_retries,
        delay_seconds=event.delay_seconds,
        status_code=error.status_code if isinstance(error, ProviderHttpError) else None,
    )


@agent_facing(topic="events")
def retry_notice(event: dict[str, Any]) -> str:
    """The line a head shows for a ``provider_retry`` event or its wire payload.

    Example: ``"HTTP 503 from model 'q' at 'http://h': loading — retry 1 of 3 in 1.2s"``.

    Args:
        event: A ``provider_retry`` payload: ``AgentEvent.model_dump()`` or a
            ``WireEvent`` dict. Reads ``error``, ``attempt``, ``max_retries`` and
            ``delay_seconds``.

    Raises:
        KeyError: If the payload lacks one of them, i.e. is not a ``provider_retry``.
    """
    return (
        f"{event['error']} — retry {event['attempt']} of {event['max_retries']} in "
        f"{float(event['delay_seconds']):.1f}s"
    )
