"""The one sentence every head shows for a ``follow_ups_dropped`` event.

See docs/TURN-SETTINGS.md §5. :func:`follow_ups_dropped_notice` is pure, so the
TUI, the REPL, ``tau -p`` and an RPC host that imports it word the drop the same way.
"""

from __future__ import annotations

from typing import Any

from tau_llm.docs import agent_facing

_PREVIEW_CHARS = 40


@agent_facing(topic="events")
def follow_ups_dropped_notice(event: dict[str, Any]) -> str:
    """The line a head shows for a ``follow_ups_dropped`` event or its wire payload.

    Args:
        event: ``AgentEvent.model_dump()`` or a ``WireEvent`` dict; reads ``dropped``.

    Returns:
        One line, e.g. ``"Aborted: dropped 1 queued follow-up: 'continue on the
        cheap model'"``. Each text is cut at 40 characters with ``…``; the event
        keeps it whole.
    """
    dropped = list(event.get("dropped") or [])
    previews = [
        repr(text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…")
        for text in dropped
    ]
    noun = "follow-up" if len(dropped) == 1 else "follow-ups"
    return f"Aborted: dropped {len(dropped)} queued {noun}: {', '.join(previews)}"
